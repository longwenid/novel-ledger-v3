from __future__ import annotations

import re
from typing import Any

from .pack import allowed_names
from ..infra.util import LedgerError, chinese_word_count
from ..ledger.ledger import lifecycle_names, norm_spatial_value


# 顶层必填键。prose 不在其中：正文真源是阶段二终稿（polished_output_path），
# submit 时由脚本读盘注入 canonical output。事实编辑不再逐字回显整章正文——
# 既省一次全章输出，也从根上消掉 assembly_prose_mismatch 这一头号返工源。
WRITE_KEYS = ("l1_summary", "state_delta", "memory", "pack_hash", "beats_hit")
QUOTE_MIN = 6
PLOT_FINDING_SEVERITIES = ("BLOCKER", "WARNING", "NIT", "UNVERIFIABLE")

# 高频组装形状错误的最小合法示例：worker 原位修复只看 hint，示例直接给形状，
# 不让它猜（实战中 memory 写成字符串、beats_hit 写成对象几乎每章复发）。
_FIELD_SHAPE_HINTS = {
    "l1_summary": "字符串：本章一句话近摘要",
    "state_delta": '对象：{"moves":[],"facts":[],"hooks":[]…}，各列表条目为对象',
    "memory": '对象不是字符串：最小合法 {"voice_concepts": []}',
    "pack_hash": "字符串：逐字回显工作包里的 pack_hash",
    "beats_hit": '纯 beat id 数组：["b1","b2"]',
}

_QUOTE_EVIDENCE_KINDS = ("facts", "debts", "hooks", "relations", "deaths", "items", "conditions", "knowledge", "locations")
_KNOWLEDGE_STANCES = frozenset({"knows", "believes", "suspects", "refuted"})


def validate_write_output(
    output: dict[str, Any],
    pack: dict[str, Any],
    *,
    enforce_word_band: bool = True,
    prose: str | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key in WRITE_KEYS:
        if key not in output:
            issues.append({
                "code": "missing_field",
                "field": key,
                "hint": f"顶层键 {key} 缺失；形状：{_FIELD_SHAPE_HINTS.get(key, '')}",
            })
    if issues:
        return issues
    # 生产提交总是由调用方传入终稿；回退只便于纯函数级校验。
    prose = prose if prose is not None else output.get("prose")
    if not isinstance(prose, str) or not prose.strip():
        issues.append({"code": "empty_prose"})
    summary = output.get("l1_summary")
    if not isinstance(summary, str) or not summary.strip():
        issues.append({"code": "empty_l1_summary"})
    delta = output.get("state_delta")
    if not isinstance(delta, dict):
        issues.append({
            "code": "state_delta_not_object",
            "hint": 'state_delta 必须是对象：{"moves":[],"facts":[],"hooks":[]…}',
        })
    memory = output.get("memory")
    if not isinstance(memory, dict):
        issues.append({
            "code": "memory_not_object",
            "hint": 'memory 必须是对象不是字符串：最小合法 {"voice_concepts": []}',
        })
    if str(output.get("pack_hash") or "") != str(pack.get("pack_hash") or ""):
        issues.append({"code": "pack_hash_mismatch"})
    if issues:
        return issues

    if enforce_word_band:
        band = pack.get("word_band") or {}
        words = chinese_word_count(prose)
        lo = int(band.get("min") or 0)
        hi = int(band.get("max") or 10**9)
        if words < lo:
            issues.append({"code": "word_count_low", "words": words, "min": lo})
        if words > hi:
            issues.append({"code": "word_count_high", "words": words, "max": hi})

    issues.extend(_beat_issues(output, pack, prose))
    issues.extend(_delta_issues(delta, pack))
    issues.extend(_delta_quote_issues(delta, prose))
    issues.extend(_location_conflict_issues(delta, pack))
    issues.extend(_expected_delta_issues(delta, pack))
    issues.extend(_continuity_issues(prose, pack))
    issues.extend(_unintroduced_familiarity_issues(prose, pack))
    issues.extend(_glossary_issues(prose, pack))

    return issues


# 外文残片闸：正文里的长拉丁字母串（模型输出事故，如 condensedcondensed 这类粘连
# 重复）在中文长篇里几乎必然是残片。曾只靠 draft prompt 自查项兜底——自查是
# 概率性的，残片是确定性的，直接机检。确需拉丁文（咒语/术语）的书用
# config set foreign_fragment_gate=allow 按书豁免（豁免判定在调用方，本函数纯判定）。
_FOREIGN_RUN_RE = re.compile(r"[A-Za-z]{6,}")
_FOREIGN_SINGLE_RUN_MIN = 8
_FOREIGN_RUN_COUNT_MAX = 2


def foreign_fragment_issues(prose: str) -> list[dict[str, Any]]:
    """确定性外文残片判定：任一 ≥8 字母的拉丁串，或 ≥3 个 ≥6 字母的拉丁串即命中。"""
    text = prose or ""
    runs = _FOREIGN_RUN_RE.findall(text)
    if not any(len(run) >= _FOREIGN_SINGLE_RUN_MIN for run in runs) and len(runs) <= _FOREIGN_RUN_COUNT_MAX:
        return []
    hits: list[dict[str, str]] = []
    for match in _FOREIGN_RUN_RE.finditer(text):
        start = max(0, match.start() - 10)
        excerpt = text[start:match.end() + 10].replace("\n", " ")
        hits.append({"run": match.group(0), "excerpt": excerpt})
        if len(hits) >= 5:
            break
    return [
        {
            "code": "foreign_fragment",
            "count": len(runs),
            "hits": hits,
            "hint": (
                "正文混入外文/乱码字母残片（模型输出事故）：按 hits 定位，删除或改写为通顺中文。"
                "draft 相位直接改 staging 草稿原文件后重新 draft-submit；组装相位按 recovery "
                "路由走 rework-patch 定点替换。"
            ),
        }
    ]


def _location_conflict_issues(delta: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    """场景地图冲突闸：申报的空间属性与地点簿注册值互斥时拦下。

    空间连续性（楼层/门牌/方位）是长跑最脆的一类——worker 每章都是新会话，
    注册表是唯一跨章记忆。冲突的两条出路都留了通道：
    - 正文错了（无意漂移）：回草稿/返工修正文；
    - 注册表该更新（有意翻修搬迁）：申报里加 "replaces": ["<属性键>"] 显式留痕，
      账本按最新声明覆盖并保留事件链。不标记就改值＝当漂移拦下。
    比较口径用 norm_spatial_value（中文数字归一，「四楼」与「4楼」同值）。
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(delta, dict):
        return issues
    registry: dict[str, dict[str, Any]] = {}
    for card in pack.get("locations") or []:
        if not isinstance(card, dict):
            continue
        for name in (str(card.get("id") or ""), str(card.get("name") or ""), *(str(a) for a in (card.get("aliases") or []))):
            if name.strip():
                registry.setdefault(name.strip(), card)
    for index, loc in enumerate(delta.get("locations") or []):
        if not isinstance(loc, dict):
            continue
        target = None
        for name in (str(loc.get("id") or ""), str(loc.get("name") or ""), *(str(a) for a in (loc.get("aliases") or []))):
            hit = registry.get(name.strip())
            if hit is not None:
                target = hit
                break
        if target is None:
            continue
        declared = loc.get("attributes") or {}
        if not isinstance(declared, dict):
            continue
        replaces = {str(k).strip() for k in (loc.get("replaces") or [])}
        registered = target.get("attributes") or {}
        for key, value in declared.items():
            old = registered.get(key)
            old_value = str((old or {}).get("value") if isinstance(old, dict) else old or "").strip()
            if not old_value:
                continue
            new_value = str(value or "").strip()
            if norm_spatial_value(old_value) == norm_spatial_value(new_value):
                continue
            if key in replaces:
                continue
            issues.append(
                {
                    "code": "location_attribute_conflict",
                    "id": str(target.get("id") or ""),
                    "attribute": key,
                    "registered": old_value,
                    "declared": new_value,
                    "quote": str(loc.get("quote") or ""),
                    "hint": (
                        f"地点「{target.get('name')}」的 {key} 已注册为「{old_value}」（第"
                        f"{(old or {}).get('chapter') if isinstance(old, dict) else '?'}章），正文写成「{new_value}」。"
                        "二选一：正文漂移→回草稿修正文；有意翻修→申报加 replaces:[\"" + str(key) + "\"] 留痕后重交"
                    ),
                }
            )
    return issues


def validate_prose_anchors(prose: str, pack: dict[str, Any]) -> list[dict[str, Any]]:
    """对一段正文跑“内容锚点”机检：beats/must、连续性、glossary。

    供段落热修补（patch_prose）等不重新走全套 submit 的路径使用，避免改写后丢掉必须发生的事件、
    把已知人物写成陌生人或引入非规范术语。
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose.strip():
        return [{"code": "empty_prose"}]
    beats = pack.get("beats") or []
    all_ids = [str(b.get("id") or "") for b in beats if b.get("required", True) and str(b.get("id") or "")]
    issues.extend(_beat_issues({"beats_hit": all_ids}, pack, prose))
    issues.extend(_continuity_issues(prose, pack))
    issues.extend(_unintroduced_familiarity_issues(prose, pack))
    issues.extend(_glossary_issues(prose, pack))
    return issues


def plot_findings_issues(output: dict[str, Any], prose: str, *, required: bool = True) -> list[dict[str, Any]]:
    """校验事实编辑的情节自检字段 `plot_findings`（事实编辑兼作剧情审校）。

    形状：[{code?, severity, hint(必填), quote}]。UNVERIFIABLE 可用 hint 声明缺失判据而不带 quote；其它级别必须有正文证据。带 quote 时必须逐字来自终稿且 ≥6 字——否则视为
    幻觉证据拒收，防止"随便报一条"绕过流水线或"报错却指不出原句"。
    返回的 issue 全部归入组装契约问题，路由回组装重跑，不误伤正文。
    """
    raw = output.get("plot_findings")
    if raw is None:
        return [{"code": "plot_findings_missing", "hint": "submit must explicitly report plot_findings; use [] after checking"}] if required else []
    if not isinstance(raw, list):
        return [{"code": "plot_findings_not_list"}]
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            issues.append({"code": "plot_finding_not_object", "index": index})
            continue
        severity = str(item.get("severity") or "").strip().upper()
        if severity not in PLOT_FINDING_SEVERITIES:
            issues.append(
                {
                    "code": "plot_finding_severity_invalid",
                    "index": index,
                    "severity": severity,
                    "hint": "plot_findings.severity 必须是 BLOCKER、WARNING、NIT 或 UNVERIFIABLE",
                }
            )
        if not str(item.get("hint") or "").strip():
            issues.append({"code": "plot_finding_missing_hint", "index": index})
        quote = str(item.get("quote") or "").strip()
        if not quote:
            if severity != "UNVERIFIABLE":
                issues.append({"code": "plot_finding_quote_missing", "index": index})
            continue
        if len(quote) < QUOTE_MIN:
            issues.append({"code": "plot_finding_quote_too_short", "index": index})
        elif not isinstance(prose, str) or quote not in prose:
            issues.append(
                {
                    "code": "plot_finding_quote_not_in_prose",
                    "index": index,
                    "quote": quote,
                    "hint": "plot_findings 的 quote 不在终稿中（幻觉证据拒收）",
                }
            )
    return issues


def collect_plot_findings(output: dict[str, Any]) -> list[dict[str, Any]]:
    """取出形状合法的情节自检条目。"""
    raw = output.get("plot_findings")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hint = str(item.get("hint") or "").strip()
        if not hint:
            continue
        out.append(
            {
                "code": str(item.get("code") or "plot_issue").strip() or "plot_issue",
                "severity": str(item.get("severity") or "").strip().upper(),
                "hint": hint,
                "quote": str(item.get("quote") or "").strip(),
            }
        )
    return out


def blocking_plot_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅 BLOCKER 触发正文返工；WARNING/NIT/UNVERIFIABLE 由调用方记录后继续。"""
    return [item for item in findings if str(item.get("severity") or "").upper() == "BLOCKER"]


# plot_findings 里可被确定性复核的发现类型：正文级 beat 锚词断言。
_BEAT_ANCHOR_FINDING_CODES = {"beat_anchor_missing", "beat_token_missing"}


def recheck_beat_anchor_blockers(
    pack: dict[str, Any], prose: str, blockers: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对现行正文确定性复检 beat 锚词类 BLOCKER，兑现了的过期发现就地清除。

    背景（实测）：plot_findings 是组装 worker 对其读到的正文的一次性判断；
    prose 返工补词后旧 BLOCKER 仍被 submit 回收，回执自相矛盾（quote 含锚词、
    hint 称全篇缺失）→ 同一发现无限回收，章死锁。规则取保守：仅当全部 required
    拍的 must 词在现行正文都已兑现时才清除锚词类发现——任何一拍仍缺就不清，
    宁可多拦一次也不放过真缺陷。非锚词类发现（人物状态/连续性判断）不可机械
    复核，原样保留。
    """
    if not any(str(f.get("code") or "") in _BEAT_ANCHOR_FINDING_CODES for f in blockers):
        return blockers, []
    issues = _beat_issues(
        {"beats_hit": [str(b.get("id") or "") for b in pack.get("beats") or []]},
        pack,
        prose,
    )
    if any(str(i.get("code") or "") == "beat_token_missing" for i in issues):
        return blockers, []
    cleared = [f for f in blockers if str(f.get("code") or "") in _BEAT_ANCHOR_FINDING_CODES]
    remaining = [f for f in blockers if str(f.get("code") or "") not in _BEAT_ANCHOR_FINDING_CODES]
    return remaining, cleared


_GLOSSARY_DEF_MARKERS = ("是指", "意为", "所谓", "即指", "出自", "又称", "的别称", "的旧称", "统称", "简称")
_GLOSSARY_DEF_PUNCT = set("，。：；、！？；：,.!?()（）")


def value_looks_like_definition(value: str) -> bool:
    """glossary 的值必须是**可直接替换的规范词串**。含标点、过长或带释义腔
    标记的值几乎一定是把 glossary 当释义词典填了——这是最常见也最伤的误用：
    键（正典词）会被机检当违禁词全书拦截。"""
    v = (value or "").strip()
    if not v:
        return False
    if any(ch in _GLOSSARY_DEF_PUNCT for ch in v):
        return True
    if len(v) >= 10:
        return True
    return any(marker in v for marker in _GLOSSARY_DEF_MARKERS)


def _glossary_entry_misused(wrong: str, preferred: str, pack: dict[str, Any]) -> bool:
    """识别「词条→释义」式误用条目：值像释义句，或键本身出现在知识库卡里
    （正典词被自己人当违禁词，自相矛盾）。误用条目在机检处跳过不拦——
    配置错误归总编在 `book audit` 的 glossary_misuse 里修，不能让写者
    在章内循环里按"违禁词"去改正典词。"""
    if value_looks_like_definition(preferred):
        return True
    if not wrong:
        return False
    for card in pack.get("kb_slice") or []:
        if not isinstance(card, dict):
            continue
        # 只认 id/title 的词条级命中：正典叙述正文里出现旧写法正是收编对象，不算误用
        blob = "".join(str(card.get(k) or "") for k in ("id", "title"))
        if wrong in blob:
            return True
    return False


def _glossary_issues(prose: str, pack: dict[str, Any]) -> list[dict[str, Any]]:
    """专有名词与术语归一化门禁：项目配置 glossary 时确保全书统一用词。"""
    glossary = pack.get("glossary") or {}
    if not isinstance(glossary, dict):
        return []
    issues: list[dict[str, Any]] = []
    for wrong_term, preferred_term in glossary.items():
        if wrong_term and preferred_term and wrong_term != preferred_term and wrong_term in prose:
            if _glossary_entry_misused(wrong_term, preferred_term, pack):
                continue
            count = prose.count(wrong_term)
            issues.append({
                "code": "glossary_term_banned",
                "wrong_term": wrong_term,
                "preferred_term": preferred_term,
                "count": count,
                "hint": f"正文中出现了非标准术语‘{wrong_term}’（共 {count} 处）！本项目统一规范用词为‘{preferred_term}’，请替换后重新提交。",
            })
    return issues


def word_count_warnings(
    output: dict[str, Any], pack: dict[str, Any], *, prose: str | None = None
) -> list[dict[str, Any]]:
    """字数带软信号：不拦提交，只提示。偏短＝本章事件（章拍）不够，补拍不注水。"""
    if prose is None:
        prose = output.get("prose")
    if not isinstance(prose, str) or not prose.strip():
        return []
    band = pack.get("word_band") or {}
    words = chinese_word_count(prose)
    lo = int(band.get("min") or 0)
    hi = int(band.get("max") or 10**9)
    warnings: list[dict[str, Any]] = []
    if lo and words < lo:
        warnings.append(
            {
                "code": "word_count_low",
                "words": words,
                "min": lo,
                "hint": "章节偏短通常是本章事件（章拍 beats）不够：先补拍加一场戏，不要注水凑字数。每章建议 5 场戏",
            }
        )
    if words > hi:
        warnings.append(
            {
                "code": "word_count_high",
                "words": words,
                "max": hi,
                "hint": "章节超出目标带：检查是否该拆成多章（上下文与 ack 成本随之上升）",
            }
        )
    return warnings


def _beat_issues(output: dict[str, Any], pack: dict[str, Any], prose: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    hits_raw = output.get("beats_hit")
    if hits_raw is None:
        hits: set[str] = set()
    elif isinstance(hits_raw, list):
        hits = {str(x) for x in hits_raw}
    else:
        return [{
            "code": "beats_hit_not_list",
            "hint": 'beats_hit 必须是纯 beat id 数组：["b1","b2"]——不要放对象或整段文本',
        }]
    for beat in pack.get("beats") or []:
        if not beat.get("required", True):
            continue
        beat_id = str(beat.get("id") or "")
        if beat_id and beat_id not in hits:
            issues.append({"code": "beat_missed", "beat_id": beat_id})

        # must 词改为语义检查：拆分成多个子词，任一出现即通过
        must = str(beat.get("must") or "").strip()
        if must:
            must_tokens = _split_must_tokens(must)
            if not any(token in prose for token in must_tokens):
                issues.append({
                    "code": "beat_token_missing",
                    "beat_id": beat_id,
                    "must": must,
                    "hint": f"beat {beat_id} 需要语义涵盖 '{must}'（可用相关词：{'/'.join(must_tokens[:5])}）"
                })
    return issues


def _split_must_tokens(must: str) -> list[str]:
    """将 must 词拆分成多个可接受的变体。

    例如：
    - "断裂肋骨" → ["断裂肋骨", "肋骨断", "断了", "断裂"]
    - "架构师" → ["架构师", "程序员", "架构"]
    - "砸门声" → ["砸门声", "砸门", "敲门"]
    """
    tokens = [must]  # 原词

    # 拆分策略：提取2-4字核心词
    if len(must) >= 3:
        for i in range(len(must) - 1):
            for j in range(i + 2, min(i + 5, len(must) + 1)):
                token = must[i:j]
                if len(token) >= 2 and token not in tokens:
                    tokens.append(token)

    # 特殊词汇映射（可扩展）
    synonyms = {
        "架构师": ["程序员", "码农", "写代码"],
        "砸门声": ["砸门", "敲门", "拍门"],
    }

    if must in synonyms:
        tokens.extend(synonyms[must])

    return tokens


def _due_field_issue(record: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """hooks/debts 的 due 必须是整数章号。

    旧 delta_schema 曾把 debts.due 文档化成「int 或 '第 N 章' 文本」，写者照合同
    写出文本章号入账后，chapter next 的到期检查裸 int() 直接崩（实测：宿主手改
    events/snapshot 七轮才洗掉毒值）。提交端拒收是第一道闸；账本侧另有归一兜底
    （coerce_due）。
    """
    due = record.get("due")
    if due is None:
        return None
    if isinstance(due, bool):
        return {
            "code": f"{kind}_due_not_int",
            "id": str(record.get("id") or ""),
            "due": due,
            "hint": "due 必须是整数章号（true/false 不是合法章号）",
        }
    if isinstance(due, int):
        return None
    text = str(due).strip()
    if text.isdigit():
        return None  # 数字字符串按合同宽容：账本侧会归一成 int
    return {
        "code": f"{kind}_due_not_int",
        "id": str(record.get("id") or ""),
        "due": due,
        "hint": (
            f"due 必须是整数章号，收到 {due!r}——把「第 N 章」一类文本改成纯数字"
            f"（如 36）；改期走 `chapter hooks defer`，不要手写文本章号"
        ),
    }


def _delta_issues(delta: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key in (
        "moves",
        "facts",
        "debts",
        "hooks",
        "relations",
        "named",
        "new_names",
        "deaths",
        "items",
        "nonliving",
        "revivals",
        "conditions",
        "knowledge",
        "locations",
    ):
        val = delta.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            issues.append({"code": f"state_delta_{key}_not_list", "field": key})
    if issues:
        return issues

    named = [str(x).strip() for x in (delta.get("named") or []) if str(x).strip()]
    new_names = {str(x).strip() for x in (delta.get("new_names") or []) if str(x).strip()}
    allowed = allowed_names(pack) | new_names

    def _unnamed(who: str) -> dict[str, Any]:
        return {
            "code": "unnamed_in_pack",
            "who": who,
            "hint": (
                f"「{who}」不在包内人名白名单：新人物先把名字写进 submission 的 new_names 再入 delta/正文，"
                "或改用白名单里的正名（不许自造称呼变体）"
            ),
        }

    for name in named:
        if name not in allowed:
            issues.append(_unnamed(name))
    for move in delta.get("moves") or []:
        if not isinstance(move, dict) or not str(move.get("who") or "").strip():
            issues.append({"code": "move_missing_who"})
            continue
        who = str(move["who"]).strip()
        if who not in allowed:
            issues.append(_unnamed(who))
        # `to` 是 moves 的唯一负载：缺了它 apply_event 会把这个人的 location 清成空串，
        # 角色随即从 occupancy 静默消失，而后续章节只看到 `location: ""`。
        if not str(move.get("to") or "").strip():
            issues.append({"code": "move_missing_to", "who": who})
    for fact in delta.get("facts") or []:
        if not isinstance(fact, dict):
            issues.append({"code": "fact_not_object", "hint": "facts items must be dicts with who, text, pin"})
            continue
        who = str(fact.get("who") or "").strip()
        text = str(fact.get("text") or "").strip()
        if not who:
            issues.append({"code": "fact_missing_who"})
        elif who not in allowed:
            issues.append(_unnamed(who))
        if not text:
            issues.append({"code": "fact_missing_text"})
    for debt in delta.get("debts") or []:
        if not isinstance(debt, dict):
            issues.append({"code": "debt_not_object"})
            continue
        if not str(debt.get("id") or "").strip():
            issues.append({"code": "debt_missing_id"})
        if not str(debt.get("text") or "").strip():
            issues.append({"code": "debt_missing_text"})
        due_issue = _due_field_issue(debt, "debt")
        if due_issue:
            issues.append(due_issue)
    for hook in delta.get("hooks") or []:
        if not isinstance(hook, dict):
            issues.append({"code": "hook_not_object"})
            continue
        if not str(hook.get("id") or "").strip():
            issues.append({"code": "hook_missing_id"})
        if not str(hook.get("text") or "").strip():
            issues.append({
                "code": "hook_missing_text",
                "hint": 'hook 条目需要 text 字段（埋下的承诺句本身，不是纯 id 引用）：'
                        '{"id":"h1","text":"掌柜欠一个答复","status":"open","due":2}',
            })
        due_issue = _due_field_issue(hook, "hook")
        if due_issue:
            issues.append(due_issue)
    for rel in delta.get("relations") or []:
        if not isinstance(rel, dict):
            issues.append({"code": "relation_not_object"})
            continue
        if not str(rel.get("who") or "").strip() or not str(rel.get("target") or "").strip() or not str(rel.get("kind") or "").strip():
            issues.append({"code": "relation_missing_fields"})
    for death in delta.get("deaths") or []:
        if isinstance(death, dict) and not str(death.get("who") or "").strip():
            issues.append({"code": "death_missing_who"})
    for kind in ("deaths", "revivals", "nonliving"):
        try:
            declared = lifecycle_names(delta.get(kind), field=kind)
        except LedgerError as exc:
            issues.append({"code": exc.code, "field": kind, "details": exc.details})
            continue
        for who in declared:
            if who not in allowed:
                issues.append(_unnamed(who))
    for item in delta.get("items") or []:
        if not isinstance(item, dict):
            issues.append({"code": "item_not_object"})
            continue
        iid = str(item.get("id") or item.get("name") or "").strip()
        if not iid:
            issues.append({"code": "item_missing_id_or_name"})
    for cond in delta.get("conditions") or []:
        if not isinstance(cond, dict):
            issues.append({"code": "condition_not_object"})
            continue
        who = str(cond.get("who") or "").strip()
        if not who:
            issues.append({"code": "condition_missing_who"})
        elif who not in allowed:
            issues.append(_unnamed(who))
        if not str(cond.get("text") or "").strip():
            issues.append({"code": "condition_missing_text"})
    seen_knowledge: set[tuple[str, str]] = set()
    for item in delta.get("knowledge") or []:
        if not isinstance(item, dict):
            issues.append({"code": "knowledge_not_object"})
            continue
        who = str(item.get("who") or "").strip()
        topic_id = str(item.get("topic_id") or "").strip()
        for field in ("who", "topic_id", "claim", "source", "quote"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                issues.append({"code": f"knowledge_missing_{field}"})
        if who and who not in allowed:
            issues.append(_unnamed(who))
        if str(item.get("stance") or "").strip() not in _KNOWLEDGE_STANCES:
            issues.append({"code": "knowledge_invalid_stance", "stance": item.get("stance")})
        key = (who, topic_id)
        if who and topic_id:
            if key in seen_knowledge:
                issues.append({"code": "knowledge_duplicate_topic", "who": who, "topic_id": topic_id})
            seen_knowledge.add(key)
    for loc in delta.get("locations") or []:
        if not isinstance(loc, dict):
            issues.append({"code": "location_not_object"})
            continue
        if not str(loc.get("id") or "").strip() and not str(loc.get("name") or "").strip():
            issues.append({"code": "location_missing_id_or_name"})
        attrs = loc.get("attributes")
        if attrs is not None and not isinstance(attrs, dict):
            issues.append({"code": "location_attributes_not_object"})
        elif isinstance(attrs, dict) and attrs:
            if not str(loc.get("quote") or "").strip():
                issues.append({"code": "location_missing_quote", "hint": "带 attributes 的地点申报必须附终稿逐字 quote"})
        status = str(loc.get("status") or "open").strip().lower()
        if status not in {"open", "closed"}:
            issues.append({"code": "location_invalid_status", "status": loc.get("status")})
        replaces = loc.get("replaces")
        if replaces is not None and not isinstance(replaces, list):
            issues.append({"code": "location_replaces_not_list"})
    return issues


def _delta_quote_issues(delta: dict[str, Any], prose: str) -> list[dict[str, Any]]:
    """账本增量的正文证据闸：语义条目带 quote 时，必须逐字命中终稿。

    事实/债务/伏笔/关系/死亡都会进入长跑账本，一锤子抽取若虚构会污染后续全部 pack；
    quote 让组装输出可以被机器核对“这条增量确实在正文里有原句支撑”。
    quote 由 expected_delta 契约强制；一般增量若提供 quote，也必须逐字命中，防止幻觉证据。
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(delta, dict) or not isinstance(prose, str):
        return issues
    for kind in _QUOTE_EVIDENCE_KINDS:
        items = delta.get(kind)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            if not quote:
                continue
            if len(quote) < QUOTE_MIN:
                issues.append(
                    {
                        "code": "delta_quote_too_short",
                        "kind": kind,
                        "index": index,
                        "hint": f"{kind}[{index}] 的 quote 太短，无法证明正文证据",
                    }
                )
            elif quote not in prose:
                issues.append(
                    {
                        "code": "delta_quote_not_in_prose",
                        "kind": kind,
                        "index": index,
                        "quote": quote,
                        "hint": f"{kind}[{index}] 的 quote 不在正文中（幻觉证据拒收）",
                    }
                )
    return issues


def _expected_name(kind: str, item: Any) -> str:
    """new_names/deaths 允许字符串或 {who} 两种写法，统一取名字。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("who") or item.get("name") or "").strip()
    return ""


def _condition_kind_matches(actual: Any, expected: Any) -> bool:
    """conditions 的 kind 匹配：计划留空才通配，计划指定时必须一致。

    `kind` 是**项目自定**自由标签（伤势/体力/欠债/职务…），制度层不携带词汇表，所以章拍
    effects 里省略 kind 是合法写法——此时不能因为输出端补了 kind 就判「未兑现」。
    但计划写了 kind 就必须一致：kind 不同在账本里是**两条** condition（见 ledger
    `_condition_key`），写错 kind 等于计划状态没落地。
    """
    act = str(actual.get("kind") or "").strip() if isinstance(actual, dict) else ""
    exp = str(expected.get("kind") or "").strip() if isinstance(expected, dict) else ""
    if not exp:
        return True
    return act == exp


def _planned_fields_match(actual: dict[str, Any], expected: dict[str, Any], kind: str) -> bool:
    """只核对计划声明的状态字段；quote 是正文证据，可由组装端补充。"""
    for field, planned_value in expected.items():
        if field == "quote" or (kind == "conditions" and field == "kind" and not str(planned_value or "").strip()):
            continue
        if field not in actual:
            return False
        actual_value = actual[field]
        if isinstance(planned_value, bool) or isinstance(actual_value, bool):
            if type(actual_value) is not type(planned_value) or actual_value != planned_value:
                return False
        elif isinstance(planned_value, str):
            if not isinstance(actual_value, str) or actual_value.strip() != planned_value.strip():
                return False
        elif actual_value != planned_value:
            return False
    return True


def _find_actual(actual_items: list[Any], kind: str, expected: dict[str, Any]) -> dict[str, Any] | None:
    """在输出 delta 里按“计划效果的稳定键”找对应实条。

    各 kind 的稳定键镜像在 infra/story_map.py 的 _EFFECTS_STABLE_KEYS（plan 侧
    落盘校验用；infra 层不得 import content）。改本函数的匹配键必须同步那张表。
    """
    for item in actual_items:
        if not isinstance(item, dict):
            if kind in ("new_names", "deaths", "revivals", "nonliving") and _expected_name(kind, item) == _expected_name(kind, expected):
                return {"who": _expected_name(kind, item)}
            continue
        if kind not in ("new_names", "deaths", "revivals", "nonliving") and not _planned_fields_match(item, expected, kind):
            continue
        if kind == "moves":
            if (
                str(item.get("who") or "").strip() == str(expected.get("who") or "").strip()
                and str(item.get("to") or "").strip() == str(expected.get("to") or "").strip()
            ):
                return item
        elif kind == "facts":
            if (
                str(item.get("who") or "").strip() == str(expected.get("who") or "").strip()
                and str(item.get("text") or "").strip() == str(expected.get("text") or "").strip()
            ):
                return item
        elif kind in ("debts", "hooks"):
            if str(item.get("id") or "").strip() == str(expected.get("id") or "").strip():
                return item
        elif kind == "relations":
            if (
                str(item.get("who") or "").strip() == str(expected.get("who") or "").strip()
                and str(item.get("target") or "").strip() == str(expected.get("target") or "").strip()
                and str(item.get("kind") or "").strip() == str(expected.get("kind") or "").strip()
            ):
                return item
        elif kind == "conditions":
            # 稳定键与账本一致：who + kind + text（kind 可留空通配，见 _condition_kind_matches）。
            # 缺这一支时，章拍只要声明 effects.conditions，组装端无论怎么写都必然被判
            # expected_delta_missing —— 不是写者没兑现，而是守卫认不出兑现。
            if (
                str(item.get("who") or "").strip() == str(expected.get("who") or "").strip()
                and str(item.get("text") or "").strip() == str(expected.get("text") or "").strip()
                and _condition_kind_matches(item, expected)
            ):
                return item
        elif kind == "knowledge":
            if (
                str(item.get("who") or "").strip() == str(expected.get("who") or "").strip()
                and str(item.get("topic_id") or "").strip() == str(expected.get("topic_id") or "").strip()
                and all(
                    str(item.get(field) or "").strip() == str(expected[field]).strip()
                    for field in ("claim", "stance", "source")
                    if expected.get(field) is not None
                )
            ):
                return item
        elif kind == "new_names":
            if _expected_name(kind, item) == _expected_name(kind, expected):
                return item
        elif kind in ("deaths", "revivals", "nonliving"):
            if _expected_name(kind, item) == _expected_name(kind, expected):
                return item
        elif kind == "items":
            planned_id = str(expected.get("id") or "").strip()
            actual_id = str(item.get("id") or "").strip()
            planned_name = str(expected.get("name") or "").strip()
            actual_name = str(item.get("name") or "").strip()
            # 双方都写了 id 就以 id 为准；章拍只给名称时允许组装补一个稳定 id。
            # 同名不同显式 id 仍是两件物品，不能按名称误认兑现。
            if planned_id and actual_id and planned_id == actual_id:
                return item
            if not planned_id and planned_name and actual_name == planned_name:
                return item
        elif kind == "locations":
            # 与 items 同款双键：双方都写 id 以 id 为准；章拍只给名称时按名称认领。
            planned_id = str(expected.get("id") or "").strip()
            actual_id = str(item.get("id") or "").strip()
            planned_name = str(expected.get("name") or "").strip()
            actual_name = str(item.get("name") or "").strip()
            if planned_id and actual_id and planned_id == actual_id:
                return item
            if not planned_id and planned_name and actual_name == planned_name:
                return item
    return None


# 近失识别键：按 kind 给出「同一条目」的宽松身份（比稳定键宽一档，只认 who/topic 级）。
# 用途：稳定键没匹配上时，把「delta 里其实有条近似的、差在哪个字段」说进回执，
# 守卫认不出兑现（形状对不上）与正文真没兑现（回草稿）两条恢复路就此分开——
# 前者回组装对齐字段（零模型），后者才动正文。实测教训：扩纲/组装 worker 为
# 一条形状不匹配翻 skill 源码、宿主手改 staging、再派一个什么都不改的 draft
# worker 只为把相位拨回去，三笔浪费都源于报错里没有近失对照。
_NEAR_MISS_KEYS: dict[str, tuple[str, ...]] = {
    "moves": ("who",),
    "facts": ("who",),
    "debts": (),
    "hooks": (),
    "relations": ("who", "target"),
    "conditions": ("who",),
    "knowledge": ("who", "topic_id"),
    "items": ("id", "name"),
    "locations": ("id", "name"),
}

# 语义状态字段不参与近失：同一身份条目在这些字段上与计划不符，就是「计划状态被改」
# （开账变销账、期限挪移、持有人变更、认知主张换向……），必须回草稿重演，不能在组装
# 侧悄悄对齐——test_matching_stable_key_cannot_hide_changed_planned_state 与知识声线
# 契约测试锁定这条守卫（knowledge 的 claim/stance/source 改写一律回草稿）。
# 近失只认 _NEAR_MISS_TEXT_FIELDS 里的自由文本字段（措辞级差异、claim/text 字段名错位、漏抄）。
_NEAR_MISS_SEMANTIC_FIELDS = frozenset({
    "status", "pin", "due", "holder", "quantity", "irreversible", "stance", "claim", "source",
})
# 允许组装侧措辞级对齐的字段：差异条目的所有差异字段都在这个集合里才算近失。
_NEAR_MISS_TEXT_FIELDS = frozenset({"text", "name"})


def _short_value(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip()
    return text if len(text) <= 60 else text[:57] + "..."


def _nearest_actual(
    actual_items: list[Any], kind: str, expected: dict[str, Any]
) -> dict[str, Any] | None:
    """找身份键一致的近失条目并给出字段级差异；没有近失返回 None。"""
    keys = _NEAR_MISS_KEYS.get(kind)
    if keys is None:
        return None
    for item in actual_items:
        if not isinstance(item, dict):
            continue
        if any(
            str(item.get(k) or "").strip() != str(expected.get(k) or "").strip()
            for k in keys
            if expected.get(k) is not None
        ):
            continue
        diffs: list[dict[str, str]] = []
        for field, planned_value in expected.items():
            if field == "quote" or field in keys:
                continue
            actual_value = item.get(field)
            if actual_value is not None and str(actual_value).strip() == str(planned_value).strip():
                continue
            if field in _NEAR_MISS_SEMANTIC_FIELDS:
                return None  # 计划状态被改：守卫优先，回草稿
            if field not in _NEAR_MISS_TEXT_FIELDS:
                return None  # 语义未知的字段差异一律不走组装侧对齐
            diffs.append(
                {
                    "field": field,
                    "expected": _short_value(planned_value),
                    "actual": _short_value(actual_value) if actual_value is not None else "<missing>",
                }
            )
        if not diffs:
            continue
        preview = {k: _short_value(item.get(k)) for k in tuple(item)[:6] if item.get(k) is not None}
        return {"nearest": preview, "diffs": diffs[:3]}
    return None


def expected_delta_duplicate_warnings(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """章拍 effects 的同章重复 knowledge 声明告警（不阻断提交，指路计划层合并）。"""
    expected = pack.get("expected_delta")
    if not isinstance(expected, dict):
        return []
    warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in expected.get("knowledge") or []:
        if not isinstance(item, dict):
            continue
        ident = (str(item.get("who") or "").strip(), str(item.get("topic_id") or "").strip())
        if not ident[0] or not ident[1]:
            continue
        if ident in seen:
            warnings.append({
                "code": "plan_effects_duplicate_topic",
                "kind": "knowledge",
                "who": ident[0],
                "topic_id": ident[1],
                "hint": (
                    "章拍 effects 同章声明了同 (who, topic_id) 的第二条 knowledge：提交端 schema "
                    "禁止同章重复（knowledge_duplicate_topic），本条在收口时已被忽略。用 "
                    "`plan patch-chapter --chapter N` 合并章拍条目消除本告警（未写章）。"
                ),
            })
        else:
            seen.add(ident)
    return warnings


def _expected_delta_issues(delta: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    """章拍 effects → expected_delta 的收口校验。

    计划里声明“这章会产生哪些状态”，组装输出必须回填计划明确声明的状态字段（可加 quote）；
    找不到说明正文没有兑现章拍（expected_delta_missing，回草稿重写），
    找到了却没带正文证据（expected_delta_quote_missing，只回组装重跑）。
    近失（身份键一致、字段对不上）单独成码 expected_delta_shape_mismatch：回组装对齐字段，
    不误伤正文——守卫认不出兑现不是写者没兑现。
    """
    expected = pack.get("expected_delta")
    if not isinstance(expected, dict) or not any(expected.values()):
        return []
    issues: list[dict[str, Any]] = []
    semantic = {"facts", "debts", "hooks", "relations", "deaths", "items", "conditions", "knowledge"}
    seen_knowledge_idents: set[tuple[str, str]] = set()
    for kind, planned_items in expected.items():
        if not isinstance(planned_items, list) or not isinstance(delta, dict):
            continue
        actual_items = delta.get(kind)
        if not isinstance(actual_items, list):
            actual_items = []
        for planned in planned_items:
            if isinstance(planned, str):
                planned = {"who": planned} if kind == "new_names" else {"who": planned}
            if not isinstance(planned, dict):
                continue
            if kind == "knowledge":
                # 同章同 (who, topic) 的重复计划条目是不可满足声明：提交端
                # knowledge_duplicate_topic 禁止第二条 delta，逐条回填则永远缺一条，
                # 任何提交都无法通过（实测整章死锁停线、被迫手改计划真源）。
                # 收口只对第一条生效（去重放行），重复本身由
                # expected_delta_duplicate_warnings 指路 plan patch-chapter 合并。
                ident = (str(planned.get("who") or "").strip(), str(planned.get("topic_id") or "").strip())
                if ident[0] and ident[1]:
                    if ident in seen_knowledge_idents:
                        continue
                    seen_knowledge_idents.add(ident)
            actual = _find_actual(actual_items, kind, planned)
            if actual is None:
                near = _nearest_actual(actual_items, kind, planned)
                if near is not None:
                    issues.append(
                        {
                            "code": "expected_delta_shape_mismatch",
                            "kind": kind,
                            "expected": planned,
                            "nearest": near["nearest"],
                            "diffs": near["diffs"],
                            "hint": (
                                "delta 里有身份一致的近失条目但字段对不上：按 diffs 对齐提交 delta 的字段值/字段名即可"
                                "（组装侧修复，零正文改动）；若核对正文后确认该事实确未演出，才升级正文返工"
                            ),
                        }
                    )
                else:
                    issues.append(
                        {
                            "code": "expected_delta_missing",
                            "kind": kind,
                            "expected": planned,
                            "hint": f"章拍 effects 声明了 {kind} 状态，但提交 delta 没有对应条目：{planned}"
                            "（delta 里没有身份键一致的条目；若正文已演出只是 delta 漏抄，属组装侧补条目；"
                            "若正文确未演出，回草稿兑现）",
                        }
                    )
                continue
            if kind in semantic and not str(actual.get("quote") or "").strip():
                issues.append(
                    {
                        "code": "expected_delta_quote_missing",
                        "kind": kind,
                        "expected": planned,
                        "hint": f"章拍 effects 的 {kind} 已命中，但输出没附正文 quote（须逐字来自终稿）",
                    }
                )
    return issues


# 「把旧识当成陌生人」的词分两类，方向性完全不同：
# - 关系型（素未谋面/初次相见…）描述**两个人之间**的关系，必须两个已知角色同时出现在窗口里才算违规；
#   「韩立与来人素未谋面」里的来人是真新角色，是合法写法，不能报。
# - 观察型（打量着眼前这个陌生…）描述的是**被观察者**，只有被观察者本身是已知角色才算违规；
#   「韩立打量着眼前这个陌生的老者」是合法写法，不能报。
_AMNESIA_RELATIONAL_PATTERNS = [
    r"素未谋面",
    r"初次相见",
    r"初次见到",
    r"头一回见",
    r"互不相识",
    r"从没见过",
]

_AMNESIA_OBSERVER_PATTERNS = [
    r"是个生面孔",
    r"打量着眼前这(?:个|名)?陌生",
    r"打量着这(?:个|名)?陌生",
    r"陌生的面孔",
]

_AMNESIA_QUERY_PATTERNS = [
    r"[“\"「](?:你是谁|你是何人|你到底是何人|报上名来|阁下尊姓大名|敢问阁下尊姓|不知阁下是哪位|在下(?:[^\n，。]{1,6})敢问道友)[？?！!]",
]

# 关系型与身份询问的取证窗口：靠"窗口里出现两个已知角色"确认这是旧识之间的失忆，
# 而不是"已知角色遇到了一个真陌生人"。
_AMNESIA_PAIR_WINDOW_BEFORE = 40
_AMNESIA_PAIR_WINDOW_AROUND = 30
# 观察型只认「陌生…<已知角色名>」的紧邻结构；窗口一放宽就会把同句里的其他人误算进来。
_AMNESIA_OBSERVER_WINDOW = 6


def _known_names_in(text: str, known: set[str]) -> set[str]:
    return {name for name in known if name and name in text}


def _nearest_known_before(text: str, pos: int, known: set[str]) -> str:
    """返回 pos 之前最近的已知角色名（用于把失忆对白归因到发问方）。"""
    best_name, best_at = "", -1
    for name in known:
        if not name:
            continue
        at = text.rfind(name, 0, pos)
        if at > best_at:
            best_name, best_at = name, at
    return best_name


def _identity_query_issues(
    prose: str, known: set[str], first_seen: dict[str, Any]
) -> list[dict[str, Any]]:
    """身份询问的方向性判定：只有「发问方与对象都是已知角色」才算失忆。

    已知角色向一个真正的新面孔问「你是何人」是合法登场写法（每次引入新人物都会出现），
    旧识之间出现这句才是穿帮。所以要求取证窗口里至少有两个已登场角色。
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for q_pat in _AMNESIA_QUERY_PATTERNS:
        for m in re.finditer(q_pat, prose):
            if m.start() in seen:
                continue
            before_start = max(0, m.start() - _AMNESIA_PAIR_WINDOW_BEFORE)
            parties = _known_names_in(prose[before_start : m.start()], known)
            if len(parties) < 2:
                # 也有人把称呼写进引号：「韩立，你是何人？」
                parties |= _known_names_in(m.group(0), known)
            if len(parties) < 2:
                continue
            seen.add(m.start())
            asker = _nearest_known_before(prose, m.start(), known) or sorted(parties)[0]
            others = "、".join(sorted(parties - {asker})) or asker
            out.append(
                {
                    "code": "continuity_character_amnesia",
                    "who": asker,
                    "matched": m.group(0),
                    "parties": sorted(parties),
                    "hint": (
                        f"角色【{asker}】与【{others}】都是已登场角色，正文却出现"
                        f"『{m.group(0)}』这类身份询问（{asker} 首见第 "
                        f"{first_seen.get(asker, '前')} 章）；确认不是把旧识写成了陌生人。"
                    ),
                }
            )
    return out


def _continuity_issues(prose: str, pack: dict[str, Any]) -> list[dict[str, Any]]:
    """检测正文中已知角色是否出现了失忆（Character Amnesia）或陌生人初见违规。
    
    若角色在先前章节已登场/与主角认识，但本章正文写成‘素未谋面’、‘初次相见’、‘你是何人’等，
    将触发 continuity_character_amnesia 门禁拦截，阻止失忆稿件入账。

    方向性：只有**两个已知角色之间**出现这类写法才算违规。已知角色打量/询问一个真新面孔
    是正常登场写法（否则每引入一个新人物都会误判返工），所以关系型与询问型都要求
    取证窗口里同时出现两个已登场角色；观察型只认「陌生…<已知角色名>」的紧邻结构。
    """
    if not prose or not isinstance(prose, str):
        return []

    known_chars: list[dict[str, Any]] = []

    # 从 present_cards 或 character_continuity 提取非首次出场的已知人物
    for card in pack.get("present_cards") or []:
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        if card.get("is_first_appearance") is False or (
            card.get("first_seen_chapter")
            and int(card.get("first_seen_chapter")) < int(pack.get("chapter") or 1)
        ):
            known_chars.append(card)

    for record in pack.get("character_continuity") or []:
        name = str(record.get("name") or "").strip()
        if not name:
            continue
        if record.get("is_first_appearance") is False and not any(
            c.get("name") == name for c in known_chars
        ):
            known_chars.append(record)

    protagonist = str((pack.get("now_card") or {}).get("name") or "").strip()
    known: set[str] = {str(c.get("name") or "").strip() for c in known_chars}
    if protagonist:
        known.add(protagonist)
    known.discard("")
    if not known:
        return []

    first_seen = {
        str(c.get("name") or "").strip(): (c.get("first_seen_chapter") or "前")
        for c in known_chars
    }

    issues: list[dict[str, Any]] = []

    for char in known_chars:
        name = str(char.get("name") or "").strip()
        if not name:
            continue

        first_ch = char.get("first_seen_chapter") or "前"
        escaped_name = re.escape(name)

        # 1. 关系型：窗口里必须同时出现两个已知角色（另一边是真陌生人就不算失忆）
        for pat in _AMNESIA_RELATIONAL_PATTERNS:
            hit: tuple[Any, list[str]] | None = None
            for m in re.finditer(pat, prose):
                window = prose[
                    max(0, m.start() - _AMNESIA_PAIR_WINDOW_AROUND) :
                    min(len(prose), m.end() + _AMNESIA_PAIR_WINDOW_AROUND)
                ]
                parties = _known_names_in(window, known)
                if name in parties and len(parties) >= 2:
                    hit = (m, sorted(parties))
                    break
            if hit:
                m, parties = hit
                others = "、".join(p for p in parties if p != name)
                issues.append({
                    "code": "continuity_character_amnesia",
                    "who": name,
                    "matched": m.group(0),
                    "parties": parties,
                    "hint": f"角色【{name}】（第 {first_ch} 章已登场）与【{others}】都是已登场角色，正文却出现‘{m.group(0)}’这类初见描写，违反人物连续性纪律！",
                })
                break

        # 2. 观察型：只有被观察者本身是已知角色才算违规
        for pat in _AMNESIA_OBSERVER_PATTERNS:
            m_obs = re.search(
                f"{pat}[^。！？\\n]{{0,{_AMNESIA_OBSERVER_WINDOW}}}{escaped_name}", prose
            )
            if m_obs:
                issues.append({
                    "code": "continuity_character_amnesia",
                    "who": name,
                    "matched": m_obs.group(0),
                    "hint": f"角色【{name}】在第 {first_ch} 章已登场，正文中却把本人写成陌生面孔（‘{m_obs.group(0)}’），违反人物连续性纪律！",
                })
                break

        # 3. 初遇/初识：同样要求两个已知角色（众人初识一个真新人是合法写法）
        m_intro = re.search(f"初[遇识]\\s*{escaped_name}", prose)
        if m_intro:
            window = prose[
                max(0, m_intro.start() - _AMNESIA_PAIR_WINDOW_AROUND) :
                min(len(prose), m_intro.end() + _AMNESIA_PAIR_WINDOW_AROUND)
            ]
            parties = _known_names_in(window, known)
            if len(parties) >= 2:
                issues.append({
                    "code": "continuity_character_amnesia",
                    "who": name,
                    "matched": m_intro.group(0),
                    "parties": sorted(parties),
                    "hint": f"角色【{name}】在第 {first_ch} 章已登场，正文中出现了‘{m_intro.group(0)}’的初识描写，违反人物连续性纪律！",
                })

    issues.extend(_identity_query_issues(prose, known, first_seen))
    return issues


_INTRO_PATTERNS = [
    r"在下",
    r"某家",
    r"老夫",
    r"我叫",
    r"自称",
    r"名叫",
    r"名为",
    r"名唤",
    r"引见",
    r"引荐",
    r"介绍",
    r"原来是",
    r"正是",
    r"认出",
    r"牌匾",
    r"招牌",
    r"名帖",
    r"腰牌",
    r"拜帖",
    r"乃是",
]


def _unintroduced_familiarity_issues(prose: str, pack: dict[str, Any]) -> list[dict[str, Any]]:
    """检测新登场角色是否被提前‘假性熟络’直呼其名（反向失忆 / 全知视角泄露）。

    若角色为首次出场（is_first_appearance=True），但在正文中没有任何引介、自报家门、
    通报姓名或辨识交代，却在对白中被直接直呼其名，将触发 continuity_unintroduced_familiarity 拦截。
    """
    if not prose or not isinstance(prose, str):
        return []

    new_chars: list[dict[str, Any]] = []
    current_chapter = int(pack.get("chapter") or 1)
    
    protagonist = str((pack.get("now_card") or {}).get("name") or "").strip()
    for card in pack.get("present_cards") or []:
        name = str(card.get("name") or "").strip()
        if not name or name == protagonist:
            continue
        first_ch = card.get("first_seen_chapter")
        if card.get("is_first_appearance") is True or (first_ch and int(first_ch) >= current_chapter):
            new_chars.append(card)

    issues: list[dict[str, Any]] = []

    for char in new_chars:
        name = str(char.get("name") or "").strip()
        if not name or len(name) < 2 or name not in prose:
            continue

        escaped_name = re.escape(name)

        has_intro = False
        for pat in _INTRO_PATTERNS:
            if re.search(f"(?:{pat}[^。！？\n]{{0,60}}{escaped_name})", prose) or re.search(f"(?:{escaped_name}[^。！？\n]{{0,60}}{pat})", prose):
                has_intro = True
                break

        if has_intro:
            continue

        dialogue_leak = re.search(
            f"[“\"「][^”\"」\n]{{0,15}}{escaped_name}(?:[，,！!？?]|啊|哈|呐|老哥|兄弟|兄台|道友|前辈)",
            prose,
        ) or re.search(
            f"(?:向|朝|对|指着|问|冲着){escaped_name}[^。！？\n]{{0,10}}[说道喊叫问][“\"「]",
            prose,
        )

        if dialogue_leak:
            issues.append({
                "code": "continuity_unintroduced_familiarity",
                "who": name,
                "matched": dialogue_leak.group(0),
                "hint": (
                    f"新角色【{name}】为首次出场，正文中没有任何自报家门、通名引介、招牌或身份交代，"
                    f"却在对白或动作中直接被直呼大名（‘{dialogue_leak.group(0)}’），"
                    "属于全知视角泄露/假性熟络穿帮，违反认知视界与悬念纪律！"
                    "修复方向：在**叙述层**补一句引介；在对白里补喊名**不算引介**——那样只会再次命中本闸。"
                    f"本闸认可的引介标记（叙述层出现任一即可，直接镜像检测器的豁免表）：{'、'.join(_INTRO_PATTERNS)}。"
                ),
            })

    return issues
