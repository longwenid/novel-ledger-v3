"""跨章数字的只读巡检内核（题材无关）。

长篇里最易漂、又最难用机检抓到的是**数**：账目加总、抽成实到、量价乘积、派生摘要
（meta/summaries/reader）里的旧值。这类错单看一章无懈可击，跨章一算就穿帮，而
字数/标点/术语/连续性机检全都看不见。本模块提供三个客观、低误报的探针：

1. `enum_sum_mismatches`：句内「A、B、C……拢共 D」的枚举求和是否等于 D（纯算术，可直接定罪）；
2. `same_key_conflicts`：项目在 `config.quant_keys` 里声明的量化键，同一章出现两个互斥数值；
3. `derived_numeric_drift`：派生摘要里的「数字+单位」是否在正文里缺席（旧值残留）。

都只报不改；是否返工由总编辑裁决。数值本身仍以项目正典/口径为准，本模块不内置任何
具体设定或题材判据。
"""

from __future__ import annotations

import re
from typing import Any

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_CN_ALL = "".join(_CN_DIGIT) + "".join(_CN_UNIT) + "万"
# 计量单位（可扩展）。含 成/分（比例）、日/天/月/年（时间）以免把它们误并进金额求和。
_UNITS = "文贯块枚斤担石亩方只条个处位成日天月年尺寸步号次回趟"
_NUM = "[" + _CN_ALL + "]"


def parse_number(token: str) -> int | None:
    """中文数词 / 阿拉伯数字 → int；无法解析返回 None。

    支持口语省略式（三百二 = 320、二百四 = 240、十五 = 15）：未出现零/〇占位时，
    末尾剩余数字按上一单位的 1/10 进位；三百零二则按明确个位解析为 302。
    """
    text = (token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    num = 0
    last_unit: int | None = None
    explicit_zero = False
    for ch in text:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            if num == 0:
                explicit_zero = True
        elif ch == "万":
            total = (total + num) * 10000
            num = 0
            last_unit = 10000
            explicit_zero = False
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if num == 0:
                num = 1
            total += num * unit
            num = 0
            last_unit = unit
            explicit_zero = False
        else:
            return None
    if num:
        # 零/〇 is a positional placeholder, not an omitted lower unit:
        # 三百二=320, but 三百零二=302 and 一万零一=10001.
        total += num * (last_unit // 10 if last_unit and not explicit_zero else 1)
    return total


_TOKEN_RE = re.compile(r"(" + _NUM + r"{1,8})\s*([" + _UNITS + r"])")
_TOTAL_RE = re.compile(
    r"(拢共|一共|合计|总共|总计|拢总|合|共)\s*(" + _NUM + r"{1,8})\s*([" + _UNITS + r"])"
)
# 句界；只在句内求和，避免跨句把无关数字并进来。
_SENT_SPLIT = re.compile(r"[。；！？!?;\n]")
# 比例/倍数表达（一成五、两成、三倍、七分）不是金额加数，先摘掉以免与相邻金额黏成一个假 token
_RATIO_RE = re.compile(_NUM + r"{1,2}(?:成|分|倍)" + _NUM + r"?")
# 共用单位的枚举（二百与三百文 → 二百文、三百文），展开后再求和
_SHARED_UNIT_RE = re.compile(r"(" + _NUM + r"{1,8})\s*([与和跟、])\s*(" + _NUM + r"{1,8})\s*([" + _UNITS + r"])")
# 资产代数流水正则（原持有 - 支出 = 结余）
_FLOW_UNITS = _UNITS + "两"
_FLOW_INITIAL_RE = re.compile(
    r"(?:原有|本有|怀揣|揣着|存有|带着|手里有|共有|总共存有|原有存银|原有本钱|原有灵石|身家共有|本钱共有)\s*(" + _NUM + r"{1,8})\s*([" + _FLOW_UNITS + r"])"
)
_FLOW_SPENT_RE = re.compile(
    r"(?:花去|用去|付了|耗费|除去|拿出|打赏|扣除|折损|使了|买[^\s，。]{1,6}花去|买[^\s，。]{1,6}用了)\s*(" + _NUM + r"{1,8})\s*([" + _FLOW_UNITS + r"])"
)
_FLOW_REMAIN_RE = re.compile(
    r"(?:还剩|剩下|仅剩|余下|结余|只剩|尚余|手里还剩|包里还剩)\s*(" + _NUM + r"{1,8})\s*([" + _FLOW_UNITS + r"])"
)


def _normalize(text: str) -> str:
    cleaned = _RATIO_RE.sub("", text or "")
    # 反复展开共用单位枚举，处理「二百与三百与四百文」链式写法
    for _ in range(4):
        new = _SHARED_UNIT_RE.sub(lambda m: f"{m.group(1)}{m.group(4)}{m.group(2)}{m.group(3)}{m.group(4)}", cleaned)
        if new == cleaned:
            break
        cleaned = new
    return cleaned


def enum_sum_mismatches(prose: str) -> list[dict[str, Any]]:
    """句内枚举求和核对：命中「…A、B、C…拢共 D」且 A+B+C≠D 即报。

    只认与 D 同单位的加数（不同单位/比例词不并入），并要求至少 2 个加数，压低误报。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return out
    for sent in _SENT_SPLIT.split(_normalize(prose)):
        for match in _TOTAL_RE.finditer(sent):
            unit = match.group(3)
            stated = parse_number(match.group(2))
            if stated is None:
                continue
            before = sent[: match.start()]
            items = [
                value
                for token, item_unit in _TOKEN_RE.findall(before)
                if item_unit == unit
                for value in (parse_number(token),)
                if value is not None
            ]
            if len(items) < 2:
                continue
            computed = sum(items)
            if computed != stated:
                out.append(
                    {
                        "code": "enum_sum_mismatch",
                        "unit": unit,
                        "items": items,
                        "computed": computed,
                        "stated": stated,
                        "quote": sent.strip(),
                    }
                )
    return out


def algebraic_flow_mismatches(prose: str) -> list[dict[str, Any]]:
    """句内代数流水核对：命中「原有 A、花去 B、还剩 C」型资产流动，校验 A - B == C。

    解决长篇小说中最常见、但枚举求和无法检测的‘初始资产 - 支出 != 结余’算术穿帮。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return out
    for sent in _SENT_SPLIT.split(_normalize(prose)):
        m_init = _FLOW_INITIAL_RE.search(sent)
        if not m_init:
            continue
        m_spent = _FLOW_SPENT_RE.search(sent, pos=m_init.end())
        if not m_spent:
            continue
        m_remain = _FLOW_REMAIN_RE.search(sent, pos=m_spent.end())
        if not m_remain:
            continue

        unit_init = m_init.group(2)
        unit_spent = m_spent.group(2)
        unit_remain = m_remain.group(2)

        # 必须是同单位计算（如都是两、块、文）
        if not (unit_init == unit_spent == unit_remain):
            continue

        a = parse_number(m_init.group(1))
        b = parse_number(m_spent.group(1))
        c = parse_number(m_remain.group(1))

        if a is None or b is None or c is None:
            continue

        computed_remain = a - b
        if computed_remain != c:
            out.append({
                "code": "algebraic_flow_mismatch",
                "unit": unit_init,
                "initial": a,
                "spent": b,
                "computed_remain": computed_remain,
                "stated_remain": c,
                "quote": sent.strip(),
                "hint": f"资产代数流水计算错误：原有 {a}{unit_init}，支出 {b}{unit_spent}，计算结余应为 {computed_remain}{unit_remain}，正文却写剩下 {c}{unit_remain}！",
            })
    return out


def _numbers_after_key(prose: str, key: str, window: int = 16) -> list[int]:
    """取「键 + 紧邻的 数值+银钱单位」——必须带单位，避免把「一成五」「两个」误当金额。"""
    values: list[int] = []
    for match in re.finditer(re.escape(key), prose):
        tail = prose[match.end() : match.end() + window]
        token = _TOKEN_RE.search(tail)
        if not token or token.group(2) not in _MONEY_UNITS:
            continue
        value = parse_number(token.group(1))
        if value is not None:
            values.append(value)
    return values


def same_key_conflicts(prose: str, keys: list[str]) -> list[dict[str, Any]]:
    """同一章内，同一量化键后跟两个不同金额即报（键由项目 `config.quant_keys` 声明）。

    键是项目层的口径名词（如「年租」「折价」），本函数不含任何题材内置词；
    未配置键时恒返回空，故默认零误报。要求数值紧跟银钱单位，压制口语量词噪声。

    **只列"每章应当单一取值"的键**：像「本利」「利钱」这类一笔一值、一章内本就可能
    出现两笔不同债务的键，列进来会产生真阳/假阳混杂（一章两大笔账是合法的），
    应交给 glossary 的禁用写法或人工复核，而不是本探针。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return out
    for key in keys or []:
        name = str(key or "").strip()
        if not name:
            continue
        values = _numbers_after_key(prose, name)
        distinct = sorted(set(values))
        if len(distinct) > 1:
            out.append({"code": "same_key_conflict", "key": name, "values": distinct})
    return out


_NUMBERED = re.compile(r"(" + _NUM + r"{1,8})\s*([" + _UNITS + r"])")
# 银钱/量数单位——派生件对账只看这类，避免把「两处/三回」等叙述量词当旧值报
_MONEY_UNITS = set("文贯块枚两斤担石亩方")
_DIGIT_CHARS = set(_CN_DIGIT) | set("0123456789")


def _reportable(number: str, unit: str) -> bool:
    """只报「含真实数字 + 银钱类单位」的 token：剔除剥离比例词产生的『百文』这类残片。"""
    return unit in _MONEY_UNITS and any(ch in _DIGIT_CHARS for ch in number)


def protected_numbers(text: str) -> set[str]:
    """抽取可对账的「数字+单位」token（用于报告展示）。"""
    if not isinstance(text, str):
        return set()
    return {
        f"{n}{u}"
        for n, u in _NUMBERED.findall(_normalize(text))
        if _reportable(n, u)
    }


def derived_numeric_drift(prose: str, derived: dict[str, str]) -> list[dict[str, Any]]:
    """派生件（meta.l1_summary / summaries / 其它）里的银钱数词，正文里查无此数即报。

    按数词的**字面**（「二百」「二十五」）在正文里做子串比对，而不是按 token 全等：
    - 「二百与三百文」改写成「二百文」这类同值改写不会误报（数词仍在正文里）；
    - 正文改尺后摘要停在旧值（如正文四百四十方、摘要仍写二百方）会因该数词在正文中
      根本不存在而被抓到。
    这是 advisory 巡检：少数因口语省略而合成出的数（正文写「二十文，又添了五文」、
    摘要写「二十五文」）会残留一两条，交总编辑 triage。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str):
        prose = ""
    haystack = _normalize(prose)
    for name, text in (derived or {}).items():
        missing: list[str] = []
        for token in sorted(protected_numbers(text)):
            number = re.sub(r"[" + _UNITS + r"]$", "", token)
            if not number or number in haystack:
                continue
            missing.append(token)
        if missing:
            out.append({"code": "derived_numeric_drift", "source": name, "missing": missing})
    return out



# 口径卡的章节标题里常带连接词/来源标注（如「符阵行工钱-承-符阵修补」），切掉尾注得候选键。
_QUAL_TITLE_TAIL = re.compile(r"[-—–_/（(].*$")
_BOLD_TERM = re.compile(r"\*\*([^*]{2,16})\*\*")


def _section_name(card_id: str, prefix: str) -> str:
    """从卡片 id（形如 `量化口径--硬-符阵行工钱-承-符阵修补`）取章节名。

    结构为 `<prefix>--<硬度>-<章节>[-<来源>]`；剥掉前缀与硬度标记后取章节名。
    """
    tail = card_id.split(prefix, 1)[-1].lstrip("-—–_")
    head, _, rest = tail.partition("-")
    section = rest if head in ("硬", "软", "中") and rest else tail
    return _QUAL_TITLE_TAIL.sub("", section).strip() or section.strip()


def propose_quant_keys(cards: list[dict[str, Any]], *, card_prefix: str = "量化口径") -> dict[str, Any]:
    """从「量化口径」类正典卡派生**候选** `quant_keys`，供总编辑勾选后落 config。

    只做建议、不自动写入：口径键的最终取舍（哪些键"每章应当单一取值"）是编辑判断，
    但把"空白页"变成"候选清单"能消除"忘了填"这一环。返回章节标题与加粗口径词两类候选。
    """
    sections: list[str] = []
    terms: list[str] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        ident = str(card.get("id") or "")
        if card_prefix not in ident:
            continue
        cleaned = _section_name(ident, card_prefix)
        if cleaned and cleaned not in sections:
            sections.append(cleaned)
        for term in _BOLD_TERM.findall(str(card.get("body") or "")):
            term = term.strip()
            if term and term not in terms:
                terms.append(term)
    return {
        "section_titles": sections,
        "bold_terms": terms,
        "candidates": sections,
        "hint": (
            "候选键只作起点：只把「每章应当单一取值」的口径名词（如年租、折价、验阵）"
            "放进 config.quant_keys；一笔一值、一章内可能有两笔不同账的键（如本利、利钱）"
            "不要放，否则 same_key_conflict 会误报。"
        ),
    }


# —— 场景地图兜底扫描：注册表无关的空间口径漂移 ——
# 地点簿（state_delta.locations）是主防线：申报即比较、replaces 即留痕。本探针是
# 收口对账的兜底——旧书没建注册表、或正文提及了从未申报的属性，跨章/章内同一地点
# 出现互斥楼层或门牌时点名。地点名来自注册表（name/aliases）或项目 config.spatial_keys，
# 本函数不内置任何地点词，未命中键时恒返回空。

_SPATIAL_FLOOR_RE = re.compile(r"(?:第)?([0-9０-９一二两三四五六七八九十]{1,4})\s*(?:楼层|楼|层)")
_SPATIAL_ROOM_RE = re.compile(r"([0-9０-９]{2,4})\s*(?:室|号房|房间|号)")
_SPATIAL_WINDOW = 40


def spatial_attribute_drift(chapters: list[tuple[int, str]], keys: list[str]) -> list[dict[str, Any]]:
    """同一地点名下出现互斥楼层/门牌即报（跨章与章内同口径）。

    chapters 是 (章号, 正文) 列表；keys 是地点名/别名。每个提及点取前后
    _SPATIAL_WINDOW 字符窗口，抽楼层与门牌模式，按地点归并后比较归一值
    （中文数字归一，「四楼」与「4楼」同值）。
    """
    from ..ledger.ledger import norm_spatial_value

    out: list[dict[str, Any]] = []
    if not keys:
        return out
    for key in keys:
        name = str(key or "").strip()
        if not name:
            continue
        seen: dict[str, dict[str, list[str]]] = {"floor": {}, "room": {}}
        for chapter, prose in chapters:
            if not isinstance(prose, str) or not prose:
                continue
            start = 0
            while True:
                index = prose.find(name, start)
                if index < 0:
                    break
                window = prose[max(0, index - _SPATIAL_WINDOW): index + len(name) + _SPATIAL_WINDOW]
                for match in _SPATIAL_FLOOR_RE.finditer(window):
                    value = norm_spatial_value(match.group(1))
                    if value:
                        seen["floor"].setdefault(value, []).append(f"第{chapter}章")
                for match in _SPATIAL_ROOM_RE.finditer(window):
                    value = norm_spatial_value(match.group(1))
                    if value:
                        seen["room"].setdefault(value, []).append(f"第{chapter}章")
                start = index + len(name)
        for kind, label in (("floor", "楼层"), ("room", "门牌")):
            values = sorted(seen[kind])
            if len(values) > 1:
                out.append(
                    {
                        "code": "spatial_attribute_drift",
                        "key": name,
                        "attribute": label,
                        "values": values,
                        "chapters": sorted({c for hits in seen[kind].values() for c in hits}),
                    }
                )
    return out
