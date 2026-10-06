"""书级事实一致性内核（题材无关、纯标准库）。

长篇里最贵的一类缺陷不是单章写错，而是**同一件事在书内有两套说法**：同一个人换了
姓氏或名字、同一个数字有两个值、同一批数目对不上、同一天在三个地方三个说法、
同一场事件前后各写一遍、上一稿的格式标记留在成稿里。单看任何一章都无懈可击，
字数/标点/术语/连续性机检全都看不见，只能靠人整本通读——而人读不完五十万字。

本模块提供的是**判据与观测的分离**：

- **观测**（本模块）：键邻近的数词、集合成员、称谓后缀、年式、清单计数、章头/标记/引号、
  跨章近似段落、全书高频片段。全部与题材无关，只认结构与数值。
- **取值域**（项目层）：`config.fact_keys` 由作者/总编按书声明。留给空表 → 恒空输出，
  默认零误报；这也是它与 `glossary` / `quant_keys` / `spatial_keys` 同款的地方。

制度层因此**零作品内容**：这里没有一个具体人名、地名、年份、势力、机制或年代的物件，
只有「一个键只允许一个取值」这一条通用纪律。具体取值永远在项目层。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .numeric_audit import _CN_ALL, _NUM, parse_number

# 键邻近观测窗口：键前后各取 N 字符找取值。太窄会漏掉「纵火那年（八九年）」这种插入语，
# 太宽会把无关数字并进来。取 24 是为了容下一个短插入语而仍留在同一小句里。
_OBSERVE_WINDOW = 24

# 数词字面与量词/比例字。这两组是**结构**判据，与题材无关。
# 字符类由有序字符串构造（不能遍历 frozenset：集合迭代序不确定，字符类会被打乱）。
_NUMERAL_CLASS = "零〇一二两三四五六七八九十百千万0123456789０１２３４５６７８９"
_NUMERAL_CHARS = frozenset(_NUMERAL_CLASS)
_UNIT_CHARS = frozenset("文贯块枚两斤担石亩方个名口条具人岁天日月年成步尺寸号次回趟倍")

# 「X成五」式比例把低位数字藏在单位后（一成五 = 15%）：先归并成单个数词「十五成」，
# 否则会被当成两个互不相干的数词（1 与 5）。通用口语式，不含题材词。
# 单字式（「两成」「一成」）保持不变，单位就是「成」。
_SCALE_RE = re.compile(r"([" + _NUMERAL_CLASS + r"])成([" + _NUMERAL_CLASS + r"])(?![0-9０-９" + _NUMERAL_CLASS + r"])")


def _normalize_scales(text: str) -> str:
    return _SCALE_RE.sub(lambda match: f"十{match.group(2)}成", text)


# 观测到的数词。要求「最长匹配」：前面不能再接数词字、后面也不能再接数词字，
# 否则切出来的只是更长数词串的碎片（「一共四」的「四」）。比例式已由 _normalize_scales
# 归一成「X成」结尾的单个数词，因此这里不把「成」写进前瞻，否则整条就匹配不上了。
_NUM_TOKEN_RE = re.compile(
    r"(?<![" + _NUMERAL_CLASS + r"])"
    r"([" + _NUMERAL_CLASS + r"]{1,8})"
    r"(?![0-9０-９" + _NUMERAL_CLASS + r"])"
    r"([\u4e00-\u9fff]{0,2})"
)

# 章头：阿拉伯或中文数字，允许全角空格与可选副标题。
_NUMERALS = "零〇一二三四五六七八九十百0-9０-９"
_ARABIC_HEADER_RE = re.compile(
    r"^[ \t　]*(?:第\s*([0-9０-９]{1,4})\s*章)([^\n]{0,40})$", re.MULTILINE
)
_CHINESE_HEADER_RE = re.compile(
    r"^[ \t　]*(?:第\s*([" + _NUMERALS + r"]{1,8})\s*章)([^\n]{0,40})$", re.MULTILINE
)

# 成稿残留：平台话术与写作期分场标记。都不是小说正文会自然出现的东西。
_RESIDUE_PATTERNS = (
    re.compile(r"[（(]\s*本章完\s*[)）]"),
    re.compile(r"[（(]\s*待续\s*[)）]"),
    re.compile(r"【\s*本章完\s*】"),
    re.compile(r"【\s*待续\s*】"),
    re.compile(r"^\s*(?:全文完|未完待续)\s*$", re.MULTILINE),
)
_SCENE_MARKER_RE = re.compile(
    r"^\s*(?:第\s*[" + _NUMERALS + r"]{1,3}\s*场(?:戏)?)[^\n]*$", re.MULTILINE
)
# 编辑提示式残留：模型偶尔把写作指令当正文留下。
_NOTE_MARKER_RE = re.compile(
    r"^\s*[\[【(（]\s*(?:注|说明|备注|提示|待补|待写|TODO|TBD)\s*[:：)）\]】]", re.MULTILINE
)

# 引号体系。key 是规范名，value 是「开/闭」字符对。
_QUOTE_SYSTEMS: dict[str, tuple[str, str]] = {
    "cn_double": ("“", "”"),
    "cn_corner": ("「", "」"),
    "zh_book": ("『", "』"),
    "ascii": ('"', '"'),
}
_QUOTE_STYLE_ORDER = ("cn_double", "cn_corner", "zh_book", "ascii")


# —— 公共命中形状 ——

def _hit(
    code: str,
    *,
    key: str,
    kind: str,
    canonical: Any,
    observed: Any,
    chapter: int,
    excerpt: str,
    hint: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "key": key,
        "kind": kind,
        "canonical": canonical,
        "observed": observed,
        "chapter": chapter,
        "excerpt": excerpt,
        "hint": hint,
    }


def _excerpt(prose: str, start: int, end: int, *, pad: int = 12) -> str:
    left = max(0, start - pad)
    right = min(len(prose), end + pad)
    return re.sub(r"\s+", " ", prose[left:right]).strip()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _canonical_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    parsed = parse_number(str(value or ""))
    return parsed


# —— 1. 实体称谓：后缀族取值域 ——

# 修饰/助词/量词字：紧贴后缀族之前出现时不属于主体称谓本身
# （「张甲一家的」里的「一」、「老李家的」里的「老」在别名里是合法组成，故不裁剪「老」）。
_MODIFIER_CHARS = frozenset("一这那某大小新旧前后两几各别的了在")
# 动词/助词字：贪婪窗口会把右邻动词收进主体（「写成李乙家」的「成」）。主体首字命中即
# 继续左裁。这是一条结构判据（中文里这些字几乎不出现在姓名/称谓首字），不含题材词。
_NON_NAME_HEAD = frozenset("成写说看想要让给把被对向从到过是在有没的得地着了和与及就都还又再")


def _suffix_variants(prose: str, suffixes: Iterable[str]) -> list[tuple[int, int, str, str]]:
    """返回 (start, end, 主体片断, 完整词) 的称谓候选。

    后缀族由项目声明（如「家/氏/宅」这类**形态**）。主体片断取后缀前的 1~3 个汉字，
    裁掉紧贴的修饰/量词起首（「张甲一家的」→ 主体「张甲」），也裁掉右邻动词造成的
    越界字（「写成李乙家」→ 主体「李乙」）。裁剪只做结构判断，不含任何题材词。
    """
    out: list[tuple[int, int, str, str]] = []
    for suffix in suffixes:
        suffix = str(suffix or "").strip()
        if not suffix:
            continue
        pattern = re.compile(r"([\u4e00-\u9fff]{1,4})" + re.escape(suffix))
        for match in pattern.finditer(prose):
            body = match.group(1)
            while body and (body[0] in _MODIFIER_CHARS or body[0] in _NON_NAME_HEAD):
                body = body[1:]
            if not body:
                continue
            start = match.start() + (len(match.group(1)) - len(body))
            out.append((start, match.end(), body, prose[start : match.end()]))
    return out


def entity_suffix_conflicts(
    prose: str,
    *,
    key: str,
    canonical: str,
    suffixes: Iterable[str],
    allow: Iterable[str] = (),
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """后缀族称谓取值域：命中既非规范前缀、也不在合法异写里的称谓。

    典型对象是「同一主体在书内被叫成两套称谓」——一套是正典写法，另一套是漂移产物。
    只认项目声明的后缀族，未声明时恒空。
    """
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose or not canonical:
        return hits
    allowed = {canonical, *_as_list(allow)}

    def _declared(body: str) -> bool:
        # 合法写法允许带修饰/量词尾巴：「张甲一家」的前缀就是规范写法「张甲」。
        return any(body.startswith(item) or item.startswith(body) for item in allowed if item)

    seen: set[str] = set()
    for start, end, body, whole in _suffix_variants(prose, suffixes):
        if _declared(body) or whole in seen:
            continue
        seen.add(whole)
        hits.append(
            _hit(
                "fact_value_conflict",
                key=key,
                kind="entity",
                canonical=canonical,
                observed=whole,
                chapter=chapter,
                excerpt=_excerpt(prose, start, end),
                hint=(
                    f"「{whole}」不在本键允许的称谓里（规范写法「{canonical}」）。"
                    "确认是同一主体的漂移称谓还是另一个主体：前者改正文，后者在申报里补合法异写。"
                ),
            )
        )
        hits[-1]["observe"] = "|".join(str(item) for item in suffixes if str(item).strip())
    return hits


# —— 2. 数值 / 3. 年式 ——

# 纯数字字面（不含量词/比例字）。观测到的 token 只要沾上量词或比例字就整条丢弃：
# 「一成五」是比例值 15 不是数列 [1,5]，「四条」里的数词部分才是 4。
_NUMERAL_CHARS = frozenset("零〇一二两三四五六七八九十百千万0123456789０１２３４５６７８９")


_UNITS_TAIL = frozenset("文贯块枚斤担石亩方个名口条具人岁天日月年步尺寸号次回趟")


def _unit_of(tail: str) -> str:
    """从数词后 0~2 字里取量词：单位字在后（成/倍）时取它，否则取首个量词字。

    「成」归一后位于数词之后（「十五成」），「倍」同理；这两种比例词是合法单位。
    其余情况先剥掉标点再取首字（「文，」→「文」）。
    """
    text = str(tail or "")
    if not text:
        return ""
    if text[0] in ("成", "倍"):
        return text[0]
    stripped = text.lstrip("，。、；：！？”」』）)")
    if not stripped:
        return ""
    return stripped[0] if stripped[0] in _UNITS_TAIL else ""


def _clean_numeral(token: str, tail: str = "") -> str | None:
    """数词字面清洗：token 只许是数字字；tail 必须能认出量词，否则整条丢弃。

    「四条人命」的 tail 是「条人」——认得出量词「条」；
    「一共四」的 tail 是「共四」——认不出量词，整条丢弃（它只是更长数词串的碎片）。
    「一成五」经 `_normalize_scales` 归一成「十五成」，tail 以「成」起首，认得出单位。
    """
    text = str(token or "")
    if not text or any(ch not in _NUMERAL_CHARS for ch in text):
        return None
    if tail and not _unit_of(tail):
        return None
    return text


def _observations(text: str) -> list[tuple[int, int, int, str]]:
    """文本里全部数词观测点：`(start, end, value, 单位字)`。

    单位字为空串表示数词后面没有可认的量词（如「他十一，记不清了」）。调用方按需要
    再筛邻近与单位一致——「一段的抽成」与「二段的抽成」不是同一个量，不能互相定罪。
    """
    out: list[tuple[int, int, int, str]] = []
    for match in _NUM_TOKEN_RE.finditer(_normalize_scales(text)):
        token = _clean_numeral(match.group(1), match.group(2))
        if token is None:
            continue
        value = parse_number(token)
        if value is None:
            continue
        tail = match.group(2)
        out.append((match.start(1), match.end(1), value, tail[:1]))
    return out


def _proximal_values(prose: str, key: str, window: int = 8) -> list[tuple[int, int, int, str, str]]:
    """键的**紧邻数词**观测点，返回 (start, end, value, 原文数词, 单位字)。

    中文里「一批东西的数目」和「键的取值」都写在键的紧邻位置：
    「四条人命」「十一岁」「一百二十文的月租」「抽成两成」。因此只在键的
    ±`window` 字符内找**与键相邻**的数词：紧挨或隔 1~2 个字（如「条」）才算。

    「一共/总共」这类总括词头本身含数词字面（「一共四」），相邻判据 + 量词起首判据
    双重挡掉，不会被当成取值。
    """
    if not key:
        return []
    normalized = _normalize_scales(prose)
    out: list[tuple[int, int, int, str, str]] = []
    for match in re.finditer(re.escape(key), normalized):
        left = max(0, match.start() - window)
        right = min(len(normalized), match.end() + window)
        region = normalized[left:right]
        anchor_end = match.start() - left
        anchor_start = match.end() - left
        best: dict[tuple[int, int], tuple[int, int, int, str, str]] = {}
        for start, end, value, unit in _observations(region):
            if end <= anchor_end:
                gap = anchor_end - end
            elif start >= anchor_start:
                gap = start - anchor_start
            else:
                gap = 0
            if gap > 2:
                continue
            position = (start, gap)
            current = best.get(position)
            if current is None or (end - start) > (current[1] - current[0]):
                best[position] = (start, end, value, region[start:end], unit)
        for start, end, value, token, unit in sorted(best.values()):
            out.append((left + start, left + end, value, token, unit))
    return out


def _numbers_near(prose: str, key: str, window: int = _OBSERVE_WINDOW) -> list[tuple[int, int, int, str, str]]:
    """跨章同键扫描用的观测：键邻近（较宽窗口）的所有数词，含单位字。

    比 `_proximal_values` 宽松：跨章同名键常隔着插入语（「押运的抽成后来又改成两成」），
    收紧到相邻会漏报。是否同量由调用方按单位字分组决定——单位不同就不是同一个量。
    """
    if not key:
        return []
    normalized = _normalize_scales(prose)
    out: list[tuple[int, int, int, str, str]] = []
    for match in re.finditer(re.escape(key), normalized):
        left = max(0, match.start() - window)
        right = min(len(normalized), match.end() + window)
        region = normalized[left:right]
        for start, end, value, unit in _observations(region):
            out.append((left + start, left + end, value, region[start:end], unit))
    return out


def number_conflicts(
    prose: str,
    *,
    key: str,
    canonical: int,
    tolerance: int = 0,
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """数值取值域：键邻近的数词与规范值不一致即报（同值异写如「二百」/「两百」不报）。"""
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose or not key:
        return hits
    canonical_value = _as_int(canonical)
    # `allow` 表示「同值的合法异写」：对本函数就是规范值本身与容差内的取值，
    # 因此逐点比较即可，不需要额外白名单。
    if canonical_value is None:
        return hits
    tol = abs(int(tolerance or 0))
    seen: set[int] = set()
    for start, end, value, token, _unit in _proximal_values(prose, key):
        if abs(value - canonical_value) <= tol or value in seen:
            continue
        seen.add(value)
        hits.append(
            _hit(
                "fact_value_conflict",
                key=key,
                kind="number",
                canonical=canonical_value,
                observed=value,
                chapter=chapter,
                excerpt=_excerpt(prose, start, end),
                hint=(
                    f"「{key}」附近出现 {value}（原文「{token}」），规范值为 {canonical_value}。"
                    "数词口径只允许一个值；改正文，或改 `fact_keys` 并留一条裁决。"
                ),
            )
        )
        hits[-1]["observe"] = key
    return hits


def count_conflicts(
    prose: str,
    *,
    key: str,
    canonical: int,
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """清单计数取值域：键邻近出现互斥的清单数目即报。

    与 `number_conflicts` 共用观测，但语义是「一批东西的数目」——同一批事物在同一处
    上下文里只能有一个总数。仍走 `fact_keys` 声明，未声明恒空。
    """
    hits = number_conflicts(prose, key=key, canonical=canonical, chapter=chapter)
    for hit in hits:
        hit["kind"] = "count"
    return hits


_ERA_TOKEN_RE = re.compile(r"([" + _NUMERALS + r"]{1,4})\s*年")


def _era_years(prose: str) -> list[tuple[int, int, str]]:
    return [
        (match.start(1), match.end(1), match.group(1))
        for match in _ERA_TOKEN_RE.finditer(prose)
    ]


def _resolve_era(raw: str, era_map: dict[str, Any] | None) -> int | None:
    """纪年文本 → 公历年。支持两位省略式（八八 → 1988）与四位式。

    基准与特例全部由项目声明 `era_map`（`{"base": 1900}` 或 `{"八九": 1989}`），
    制度层不假设任何年代的起讫。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if era_map:
        if text in era_map and isinstance(era_map[text], int):
            return int(era_map[text])
    digits = _arabic_or_cn(text)
    if digits is None:
        return None
    if len(str(digits)) >= 4:
        return digits
    base = 1900
    if isinstance((era_map or {}).get("base"), int):
        base = int(era_map["base"])
    century = base - (base % 100)
    candidate = century + digits
    if candidate < base:
        candidate += 100
    return candidate


def _arabic_or_cn(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    full = str.maketrans("０１２３４５６７８９", "0123456789")
    translated = text.translate(full)
    if translated.isdigit():
        return int(translated)
    # 逐字读法（把「八八」读成 88、「一九九三」读成 1993）
    single = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
              "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    if all(ch in single for ch in text) and text:
        return int("".join(single[ch] for ch in text))
    return parse_number(text)


def date_year_conflicts(
    prose: str,
    *,
    key: str,
    canonical: int,
    era_map: dict[str, Any] | None = None,
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """年式取值域：键邻近的年式归一后与规范年不一致即报。

    中文数字年、阿拉伯年、省略式纪年（由 `era_map` 提供基准）统一归一到公历年再比较，
    所以「同一年被写成两种写法」不会误报，「两个不同年份」一定报。
    """
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose or not key:
        return hits
    canonical_year = _as_int(canonical)
    if canonical_year is None:
        return hits
    seen: set[int] = set()
    for match in re.finditer(re.escape(key), prose):
        left = max(0, match.start() - _OBSERVE_WINDOW)
        right = min(len(prose), match.end() + _OBSERVE_WINDOW)
        region = prose[left:right]
        for start, end, raw in _era_years(region):
            year = _resolve_era(raw, era_map)
            if year is None or year == canonical_year or year in seen:
                continue
            seen.add(year)
            hits.append(
                _hit(
                    "fact_value_conflict",
                    key=key,
                    kind="date",
                    canonical=canonical_year,
                    observed=year,
                    chapter=chapter,
                    excerpt=_excerpt(prose, left + start, left + end),
                    hint=(
                        f"「{key}」附近出现 {raw} 年（归一为 {year}），规范年为 {canonical_year}。"
                        "同一时点只允许一个年份；改正文，或补 `era_map` 说明这是另一个时点。"
                    ),
                )
            )
            hits[-1]["observe"] = key
    return hits


# —— 4. 互斥集合成员 ——

def set_member_conflicts(
    prose: str,
    *,
    key: str,
    canonical: str,
    allow: Iterable[str] = (),
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """互斥集合取值域：键邻近出现本键允许集合之外的成员即报。

    适用于「二选一且互斥」的取值（方位、内外、前后、编号档位……）。取值集合由项目在
    `canonical` + `allow` 里声明；两边都出现就是真冲突，只有非规范那个出现也报。
    """
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose or not key:
        return hits
    values = [_canonical_text(canonical), *_as_list(allow)]
    values = [value for value in values if value]
    if not values:
        return hits
    canonical_value = values[0]
    seen: set[str] = set()
    for match in re.finditer(re.escape(key), prose):
        left = max(0, match.start() - _OBSERVE_WINDOW)
        right = min(len(prose), match.end() + _OBSERVE_WINDOW)
        region = prose[left:right]
        for value in values[1:]:
            for found in re.finditer(re.escape(value), region):
                if value in seen:
                    continue
                seen.add(value)
                hits.append(
                    _hit(
                        "fact_value_conflict",
                        key=key,
                        kind="set",
                        canonical=canonical_value,
                        observed=value,
                        chapter=chapter,
                        excerpt=_excerpt(prose, left + found.start(), left + found.end()),
                        hint=(
                            f"「{key}」附近同时/单独出现「{value}」，同处上下文只允许一个取值"
                            f"（先声明的是「{canonical_value}」）。核对正文后统一。"
                        ),
                    )
                )
                hits[-1]["observe"] = key
    return hits


# —— 5. 实体名与别名 ——

# 功能字：贪婪匹配常把「别名 + 动词」切成四字串（「老赵翻过」）。人名/称谓极少以这些字
# 收尾或居中，出现即判为切分噪声而不是另一个名字。这是一条**结构**判据，不含题材词。
_FUNCTION_CHARS = frozenset(
    "的了着和与及也就都还又再很是不在把被让给对向从到过说看着想会能要做有没里上下来去"
)

def entity_name_conflicts(
    prose: str,
    *,
    key: str,
    canonical: str,
    aliases: Iterable[str] = (),
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """实体名取值域：规范名与合法别名之外，出现键邻近的另一个名字即报。

    别名与身份揭示是合法写法，所以必须由项目显式声明 `aliases`；没声明的另一个名字
    就是漂移，报出来让人裁决。这条与 SKILL.md「人物账本统一使用稳定规范名」同源。

    只报**近形名**（与规范名共享起首字、且不是规范名的子串），把「张甲」与「张丙」
    这类真正会混淆的漂移报出来，避免把同句里的任意词当人名。每章每键最多报 3 条，
    防止长章里刷屏。
    """
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose or not key or not canonical:
        return hits
    known = {canonical, *_as_list(aliases)}
    # 候选起首字取「规范名首字」与「已有合法别名首字」的并集：这样别名本身不会被
    # 当成另一个名字，而别名之后的动词也不会被贪婪切成名字。
    heads = sorted({item[0] for item in known if item})
    heads = [item for item in heads if "\u4e00" <= item <= "\u9fff"]
    if not heads:
        return hits
    pattern = re.compile("[" + "".join(re.escape(item) for item in heads) + r"][\u4e00-\u9fff]{1,3}")
    seen: set[str] = set()
    for match in re.finditer(re.escape(key), prose):
        if len(hits) >= 3:
            break
        left = max(0, match.start() - _OBSERVE_WINDOW)
        right = min(len(prose), match.end() + _OBSERVE_WINDOW)
        region = prose[left:right]
        reported = False
        for found in pattern.finditer(region):
            raw = found.group(0)
            # 贪婪匹配会把「赵敬之写的」切成五字串：取可声明的最长前缀（≤4 字）当名字，
            # 已知写法（规范名/别名）优先取最长匹配，避免把别名后的动词并进名字。
            limit = min(len(raw), max(len(item) for item in known))
            variants = [raw[:size] for size in range(limit, 1, -1)]
            candidate = ""
            for variant in variants:
                if variant in known:
                    candidate = variant
                    break
            if not candidate:
                candidate = variants[0] if variants else raw
            if candidate in known or candidate in seen:
                continue
            if candidate.startswith(canonical) or canonical.startswith(candidate):
                continue
            if any(ch in _FUNCTION_CHARS for ch in candidate):
                continue  # 贪婪匹配切到功能字（「老赵翻过」）→ 不是名字
            seen.add(candidate)
            hits.append(
                _hit(
                    "fact_value_conflict",
                    key=key,
                    kind="entity",
                    canonical=canonical,
                    observed=candidate,
                    chapter=chapter,
                    excerpt=_excerpt(prose, left + found.start(), left + found.start() + len(candidate)),
                    hint=(
                        f"「{key}」附近出现「{candidate}」，与规范名「{canonical}」起首相同但不在 `aliases` 里。"
                        "若确为同一主体的别名或身份揭示，补进 `aliases`；否则改正文。"
                    ),
                )
            )
            hits[-1]["observe"] = key
            reported = True
            break  # 一个键命中处只报一个近形名，避免同句刷屏
        if reported:
            continue
    return hits


# —— 6. 声明表驱动 ——

def fact_key_conflicts(
    prose: str,
    spec: dict[str, Any],
    *,
    key: str,
    chapter: int = 0,
) -> list[dict[str, Any]]:
    """按一条 `fact_keys` 声明扫描一段正文。未声明形状 → 恒空（默认零误报）。

    命中里的 `key` 一律回填**声明的键名**（不是观测词），`observe` 另存观测词：
    总编辑按 key 就能在配置里找到那一条声明，按 observe 才知道正文里按什么找的。
    """
    if not isinstance(spec, dict) or not isinstance(prose, str) or not prose:
        return []
    kind = str(spec.get("kind") or "").strip().lower()
    observe = str(spec.get("observe") or spec.get("key") or "").strip()
    canonical = spec.get("canonical")
    if canonical is None or str(canonical).strip() == "":
        return []
    if kind == "entity":
        suffixes = _as_list(spec.get("suffixes"))
        if suffixes:
            return _declared(
                entity_suffix_conflicts(
                    prose,
                    key=key,
                    canonical=_canonical_text(canonical),
                    suffixes=suffixes,
                    allow=_as_list(spec.get("allow")) + _as_list(spec.get("aliases")),
                    chapter=chapter,
                ),
                key=key,
                observe="|".join(suffixes),
            )
        if observe:
            return _declared(
                entity_name_conflicts(
                    prose,
                    key=observe,
                    canonical=_canonical_text(canonical),
                    aliases=_as_list(spec.get("aliases")) + _as_list(spec.get("allow")),
                    chapter=chapter,
                ),
                key=key,
                observe=observe,
            )
        return []
    if not observe:
        return []
    if kind == "number":
        return _declared(
            number_conflicts(
                prose,
                key=observe,
                canonical=_as_int(canonical),
                tolerance=_as_int(spec.get("tolerance")) or 0,
                chapter=chapter,
            ),
            key=key,
            observe=observe,
        )
    if kind == "count":
        return _declared(
            count_conflicts(prose, key=observe, canonical=_as_int(canonical), chapter=chapter),
            key=key,
            observe=observe,
        )
    if kind == "date":
        return _declared(
            date_year_conflicts(
                prose,
                key=observe,
                canonical=_as_int(canonical),
                era_map=spec.get("era_map") if isinstance(spec.get("era_map"), dict) else None,
                chapter=chapter,
            ),
            key=key,
            observe=observe,
        )
    if kind == "set":
        return _declared(
            set_member_conflicts(
                prose,
                key=observe,
                canonical=_canonical_text(canonical),
                allow=_as_list(spec.get("allow")),
                chapter=chapter,
            ),
            key=key,
            observe=observe,
        )
    return []


def _declared(hits: list[dict[str, Any]], *, key: str, observe: str) -> list[dict[str, Any]]:
    """把检测器内部用的观测词换成声明的键名，并保留观测词备查。"""
    for hit in hits:
        hit["observe"] = hit.get("observe") or observe
        hit["key"] = key
    return hits


def declared_fact_conflicts(
    fact_keys: dict[str, Any],
    prose: str,
    *,
    chapter: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按声明表的 `policy` 把命中分成两桶，供提交闸分流。

    - `policy: "fixed"`（默认）——v2 锚定的纪律：该取值全书只有一个，正文与声明不符
      是**硬错**，提交闸当场拒（回正文返工）。逃生口只有两条：改正文，或总编辑显式
      改声明并留裁决（对应 v2 的 `anchor_change` 带 reason/evidence）。
    - `policy: "eventful"`——该取值在书内可以合法变动（职务、住址、身份揭示），命中
      只作 advisory 记录，交总编辑裁决，不挡线。

    返回 `(fixed_hits, eventful_hits)`；`fact_keys` 为空时两桶恒空（默认零误报）。
    """
    fixed: list[dict[str, Any]] = []
    eventful: list[dict[str, Any]] = []
    for key, spec in sorted((fact_keys or {}).items()):
        if not isinstance(spec, dict):
            continue
        hits = fact_key_conflicts(prose, spec, key=key, chapter=chapter)
        if not hits:
            continue
        bucket = eventful if str(spec.get("policy") or "fixed").strip().lower() == "eventful" else fixed
        bucket.extend(hits)
    return fixed, eventful


def _story_year(chapter: int, chapter_years: dict[Any, Any]) -> int | None:
    """章号 → 故事年：取已声明锚点里 ≤ 本章的最近一个。没有锚点 → None（不检查）。"""
    best: tuple[int, int] | None = None
    for raw_chapter, raw_year in (chapter_years or {}).items():
        try:
            anchor_chapter = int(raw_chapter)
            anchor_year = int(raw_year)
        except (TypeError, ValueError):
            continue
        if anchor_chapter > chapter:
            continue
        if best is None or anchor_chapter > best[0]:
            best = (anchor_chapter, anchor_year)
    return best[1] if best else None


_AGE_WINDOW = 12


def age_anchor_conflicts(
    prose: str,
    *,
    chapter: int = 0,
    chapter_years: dict[Any, Any] | None = None,
    birth_years: dict[str, Any] | None = None,
    tolerance: int = 0,
) -> list[dict[str, Any]]:
    """年龄算术锚（吸收 v2「出生日期用 fixed anchor」的最小可用形态）。

    项目声明 `chapter_years`（章号 → 故事年）与 `birth_years`（人名 → 出生年）后，
    正文里「<人名>…N岁」的 N 必须等于 `故事年 − 出生年`（±tolerance）。年龄从此是
    **算出来的**，不再靠每章现编——同一人物年龄链前后矛盾这一类硬伤在声明完备时
    被算术直接定罪。任一锚点未声明则恒空（默认零误报）。
    """
    hits: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return hits
    year = _story_year(chapter, chapter_years or {})
    if year is None:
        return hits
    tol = abs(int(tolerance or 0))
    for name, raw_birth in (birth_years or {}).items():
        person = str(name or "").strip()
        if not person or len(person) < 2:
            continue
        try:
            birth = int(raw_birth)
        except (TypeError, ValueError):
            parsed_birth = parse_number(str(raw_birth or ""))
            if parsed_birth is None:
                continue
            birth = parsed_birth
        expected = year - birth
        start = 0
        reported_for_person = False
        while not reported_for_person:
            at = prose.find(person, start)
            if at < 0:
                break
            region = prose[at + len(person) : at + len(person) + _AGE_WINDOW]
            for token_match in _NUM_TOKEN_RE.finditer(region):
                token = _clean_numeral(token_match.group(1), token_match.group(2))
                if token is None:
                    continue
                tail = token_match.group(2)
                if not tail or tail[0] != "岁":
                    continue
                value = parse_number(token)
                if value is None:
                    continue
                if abs(value - expected) <= tol:
                    continue
                hits.append(
                    _hit(
                        "age_anchor_conflict",
                        key=person,
                        kind="age",
                        canonical=expected,
                        observed=value,
                        chapter=chapter,
                        excerpt=_excerpt(prose, at, at + len(person) + token_match.end()),
                        hint=(
                            f"按时间锚推算，「{person}」在故事第 {year} 年应为 {expected} 岁"
                            f"（出生年 {birth}），正文却写 {value}。年龄由锚点算出："
                            "改正文，或总编辑改时间锚并留裁决。"
                        ),
                    )
                )
                reported_for_person = True
                break
            start = at + len(person)
    return hits


# —— 7. 跨章同键双值 ——

def cross_chapter_key_conflicts(
    chapters: list[tuple[int, str]],
    keys: Iterable[str],
) -> list[dict[str, Any]]:
    """同一量化键在**全书**出现两个互斥取值即报（章内版见 numeric_audit.same_key_conflicts）。

    章内只比一章，跨章才是长篇真正的漂移形态：同一键在第 3 章是 120、第 90 章是 150。
    键由项目 `config.quant_keys` 声明，未声明恒空。
    """
    out: list[dict[str, Any]] = []
    for key in keys or []:
        name = str(key or "").strip()
        if not name:
            continue
        # 按单位分组：单位不同的两个数不是同一个量（「一段」与「二段」、「两成」与「两百文」），
        # 互相定罪只会制造假阳。只有同量纲的两个取值才算键的互斥取值。
        by_unit: dict[str, dict[int, list[int]]] = {}
        for chapter, prose in chapters:
            if not isinstance(prose, str) or not prose:
                continue
            for _start, _end, value, _token, unit in _numbers_near(prose, name):
                by_unit.setdefault(unit, {}).setdefault(value, []).append(int(chapter))
        for unit, values in sorted(by_unit.items()):
            if len(values) < 2:
                continue
            out.append(
                {
                    "code": "cross_chapter_key_conflict",
                    "key": name,
                    "unit": unit,
                    "values": sorted(values),
                    "chapters_by_value": {
                        str(value): sorted(set(chapters_list))
                        for value, chapters_list in sorted(values.items())
                    },
                    "hint": (
                        f"「{name}」在全书出现 {len(values)} 个不同取值"
                        f"（{', '.join(str(v) for v in sorted(values))}{unit}）。"
                        "同一口径键全书只允许一个值：定权威值后改正文，或改口径并留裁决。"
                    ),
                }
            )
    return out


# —— 8. 章节格式与体例 ——

def quote_style_issues(
    prose: str,
    *,
    chapter: int = 0,
    canonical_style: str = "auto",
) -> list[dict[str, Any]]:
    """引号体系：章内混用与配对不闭合。`auto` 表示只判「同一章内只准一种体系」。"""
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return out
    counts = {
        name: (prose.count(pair[0]), prose.count(pair[1]))
        for name, pair in _QUOTE_SYSTEMS.items()
    }
    used = [name for name in _QUOTE_STYLE_ORDER if counts[name][0] or counts[name][1]]
    for name in used:
        opened, closed = counts[name]
        if name == "ascii":
            if opened % 2 != 0:
                out.append(
                    {
                        "code": "quote_unpaired",
                        "chapter": chapter,
                        "style": name,
                        "opened": opened,
                        "closed": closed,
                        "hint": "直引号数量为奇数：引号未闭合或漏了配对，逐处核对后重交。",
                    }
                )
            continue
        if opened != closed:
            out.append(
                {
                    "code": "quote_unpaired",
                    "chapter": chapter,
                    "style": name,
                    "opened": opened,
                    "closed": closed,
                    "hint": f"引号未闭合：{name} 开 {opened} 个、闭 {closed} 个，逐处核对后重交。",
                }
            )
    if canonical_style and canonical_style != "auto":
        offending = [name for name in used if name != canonical_style]
        if offending:
            out.append(
                {
                    "code": "quote_style_mixed",
                    "chapter": chapter,
                    "style": canonical_style,
                    "found": offending,
                    "hint": f"本项目引号体例已锁定为 {canonical_style}，本章出现 {offending}：统一体例后重交。",
                }
            )
        return out
    if len(used) > 1:
        out.append(
            {
                "code": "quote_style_mixed",
                "chapter": chapter,
                "style": "auto",
                "found": used,
                "hint": (
                    f"同一章内混用 {len(used)} 套引号体例（{used}）：全书体例必须统一，"
                    "改完重交；确需混排时用 `config set --key quote_style` 显式锁定体例。"
                ),
            }
        )
    return out


def chapter_format_issues(
    prose: str,
    *,
    chapter: int = 0,
    quote_style: str = "auto",
) -> list[dict[str, Any]]:
    """章节级格式与体例机检：章头、成稿残留、分场标记、裸标题行、引号体例。

    这些都是**确定性**缺陷：小说正文不会自然出现「第三场」「（本章完）」或同章两小节
    相同的章头。因此可以直接当硬闸，不需要人工 triage。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(prose, str) or not prose:
        return out
    headers = _chapter_headers(prose)
    if len(headers) > 1:
        out.append(
            {
                "code": "duplicated_chapter_header",
                "chapter": chapter,
                "headers": [text for _start, _end, text in headers][:5],
                "hint": "本章出现多个章头行（多半是改写残留的重复章头）：只保留一个，删掉多余行后重交。",
            }
        )
    elif len(headers) == 1 and chapter:
        text = headers[0][2]
        header_number = _header_number(text)
        if header_number is not None and header_number != chapter:
            out.append(
                {
                    "code": "chapter_header_mismatch",
                    "chapter": chapter,
                    "header": text,
                    "header_number": header_number,
                    "hint": f"章头写的是第 {header_number} 章，实际提交的是第 {chapter} 章：改章头后重交。",
                }
            )
    for pattern in _RESIDUE_PATTERNS:
        for match in pattern.finditer(prose):
            out.append(
                {
                    "code": "story_marker_residue",
                    "chapter": chapter,
                    "text": match.group(0).strip(),
                    "hint": "成稿里残留写作期标记（本章完/待续类）：删掉后重交。",
                }
            )
    for match in _SCENE_MARKER_RE.finditer(prose):
        out.append(
            {
                "code": "scene_break_marker",
                "chapter": chapter,
                "text": match.group(0).strip(),
                "hint": "正文残留写作期分场标记（第 N 场）：删掉标记，让场景靠叙事转场衔接。",
            }
        )
    for match in _NOTE_MARKER_RE.finditer(prose):
        out.append(
            {
                "code": "story_marker_residue",
                "chapter": chapter,
                "text": match.group(0).strip(),
                "hint": "正文残留编辑提示式标记（注/待补类）：删掉后重交。",
            }
        )
    bare = _bare_title_lines(prose)
    for text in bare:
        out.append(
            {
                "code": "bare_scene_heading",
                "chapter": chapter,
                "text": text,
                "hint": "正文出现单独成行的裸标题：删掉或改写进叙述。",
            }
        )
    out.extend(quote_style_issues(prose, chapter=chapter, canonical_style=quote_style))
    return out


def _chapter_headers(prose: str) -> list[tuple[int, int, str]]:
    """章头行：`(start, end, 整行文本)`，按出现顺序。"""
    found: list[tuple[int, int, str]] = []
    for pattern in (_ARABIC_HEADER_RE, _CHINESE_HEADER_RE):
        for match in pattern.finditer(prose):
            found.append((match.start(), match.end(), match.group(0).strip()))
    found.sort(key=lambda item: item[0])
    # 去重：同一行不会被两个模式同时命中（阿拉伯/中文数字集合不同），但空行与全角空格可能重复
    deduped: list[tuple[int, int, str]] = []
    for item in found:
        if deduped and deduped[-1][0] == item[0]:
            continue
        deduped.append(item)
    return deduped


def _header_number(text: str) -> int | None:
    match = _ARABIC_HEADER_RE.match(text)
    if match:
        return _arabic_or_cn(match.group(1))
    match = _CHINESE_HEADER_RE.match(text)
    if match:
        return _arabic_or_cn(match.group(1))
    return None


def chapter_header_style_issues(chapters: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """章号体例：全书章头必须同一种写法（纯阿拉伯或纯中文数字）。"""
    styles: dict[str, list[int]] = {}
    for chapter, prose in chapters:
        if not isinstance(prose, str):
            continue
        for _start, _end, text in _chapter_headers(prose)[:1]:
            if _ARABIC_HEADER_RE.match(text):
                styles.setdefault("arabic", []).append(int(chapter))
            elif _CHINESE_HEADER_RE.match(text):
                styles.setdefault("chinese", []).append(int(chapter))
    if len(styles) < 2:
        return []
    return [
        {
            "code": "chapter_header_style_mixed",
            "styles": {name: sorted(set(values))[:8] for name, values in sorted(styles.items())},
            "hint": "全书章号体例不统一（阿拉伯与中文数字混用）：选定一种后统改章头。",
        }
    ]


def _bare_title_lines(prose: str) -> list[str]:
    """裸标题行：整行只有 2~8 个汉字、无标点、且不是章头。

    真实正文段几乎不会只有两三个汉字加换行；出现即多半是构思期的标题残留。
    """
    out: list[str] = []
    for line in prose.splitlines():
        text = line.strip()
        if not text or len(text) > 8:
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", text):
            continue
        if _CHINESE_HEADER_RE.match(text) or _ARABIC_HEADER_RE.match(text):
            continue
        if any(ch in text for ch in "，。！？；：、“”「」（）"):
            continue
        out.append(text)
        if len(out) >= 3:
            break
    return out


# —— 9. 跨章近重复段落 ——

# 高频虚词：相似度计算前剔除，避免「他说/然后/一个」这类词把不相关段落抬到高相似。
_STOP_CHARS = frozenset("的了着和与及也就都还又再很是不没有一二三四五六七八九十")


def _ngrams(text: str, size: int = 3) -> frozenset[str]:
    cleaned = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    cleaned = "".join(ch for ch in cleaned if ch not in _STOP_CHARS)
    if len(cleaned) < size:
        return frozenset({cleaned}) if cleaned else frozenset()
    return frozenset(cleaned[i : i + size] for i in range(len(cleaned) - size + 1))


def near_duplicate_passages(
    chapters: list[tuple[int, str]],
    *,
    min_hans: int = 60,
    max_hans: int = 400,
    threshold: float = 0.85,
    cap: int = 40,
    window_sentences: int = 4,
) -> list[dict[str, Any]]:
    """跨章近重复段落：同一段叙述在两个位置出现（整场事件被写两遍的机器信号）。

    句对齐窗口 + 汉字 3-gram 重叠率（重叠除以较小集合，与卷间复读探针同口径）；阈值取高
    （默认 0.85）宁可漏报也不误伤合法的复现写法（重复咏叹、每日例行）。只报**跨章**重复，
    章内自重复不在此列；两个位置各自只出一次报告，避免滑动窗口把同一处重复刷成一串。

    窗口按**固定句数**（`window_sentences`）而不是字数上界切分，否则同一个重复会被
    任意长度的窗口切出大量近似片段。
    """
    spans: list[dict[str, Any]] = []
    for chapter, prose in chapters:
        if not isinstance(prose, str) or not prose:
            continue
        sentences = [
            (match.start(), match.end())
            for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", prose)
            if match.group(0).strip()
        ]
        for index in range(len(sentences)):
            start = sentences[index][0]
            end = sentences[min(index + window_sentences - 1, len(sentences) - 1)][1]
            text = prose[start:end]
            hans = len(re.findall(r"[\u4e00-\u9fff]", text))
            if hans < min_hans or hans > max_hans:
                continue
            grams = _ngrams(text)
            if not grams:
                continue
            spans.append({"span": (int(chapter), start, end), "grams": grams, "text": text})
    out: list[dict[str, Any]] = []
    used: set[tuple[int, int, int]] = set()
    for i in range(len(spans)):
        span_a = spans[i]
        if span_a["span"] in used:
            continue
        for j in range(i + 1, len(spans)):
            span_b = spans[j]
            if span_a["span"][0] == span_b["span"][0] or span_b["span"] in used:
                continue
            smaller = min(len(span_a["grams"]), len(span_b["grams"]))
            if not smaller:
                continue
            overlap = len(span_a["grams"] & span_b["grams"]) / smaller
            if overlap < threshold:
                continue
            used.add(span_a["span"])
            used.add(span_b["span"])
            out.append(
                {
                    "code": "near_duplicate_passage",
                    "chapters": [span_a["span"][0], span_b["span"][0]],
                    "similarity": round(overlap, 3),
                    "excerpt": re.sub(r"\s+", " ", span_a["text"])[:80],
                    "hint": (
                        f"第 {span_a['span'][0]} 章与第 {span_b['span'][0]} 章出现高相似叙述"
                        f"（{overlap:.0%}）：确认是同一场事件被写了两遍（删一处或改写其中一处）"
                        "还是刻意的复现写法（留一条裁决）。"
                    ),
                }
            )
            break
        if len(out) >= cap:
            break
    return out


# —— 10. 全书高频片段 ——

def repeated_phrases(
    chapters: list[tuple[int, str]],
    *,
    n: int = 4,
    min_count: int = 6,
    cap: int = 20,
) -> list[dict[str, Any]]:
    """全书高频片段榜：把「作者口头禅」从读感问题变成可见计数。

    只统汉字连续片段（跨标点不算），因此不会把正常搭配误算成复读；超出 `min_count`
    的按次数排序取前 `cap` 条。这是 advisory：密度本身不是错误，裁决权在总编。
    """
    counts: dict[str, int] = {}
    chapters_by_phrase: dict[str, set[int]] = {}
    for chapter, prose in chapters:
        if not isinstance(prose, str) or not prose:
            continue
        for run in re.findall(r"[\u4e00-\u9fff]{%d,}" % n, prose):
            for size in (n, min(n + 2, 8)):
                if len(run) < size:
                    continue
                for i in range(len(run) - size + 1):
                    phrase = run[i : i + size]
                    counts[phrase] = counts.get(phrase, 0) + 1
                    chapters_by_phrase.setdefault(phrase, set()).add(int(chapter))
    ranked = sorted(
        (item for item in counts.items() if item[1] > min_count),
        key=lambda item: (-item[1], item[0]),
    )
    out: list[dict[str, Any]] = []
    for phrase, count in ranked[:cap]:
        out.append(
            {
                "code": "repeated_phrase",
                "phrase": phrase,
                "count": count,
                "chapters": len(chapters_by_phrase.get(phrase, ())),
                "hint": (
                    f"片段「{phrase}」全书出现 {count} 次（分布在 {len(chapters_by_phrase.get(phrase, ()))} 章）："
                    "密度可能已到反噬点，按总编裁决降频，不要机械删除。"
                ),
            }
        )
    return out


# —— 统一入口 ——

def format_and_fact_issues(
    prose: str,
    cfg: dict[str, Any],
    *,
    chapter: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """提交闸一次跑完格式 + 事实取值域 + 时间锚，按 `policy` 分流。

    返回 `(hard_issues, fact_eventful, fact_fixed)`：
    - `hard_issues`：确定性格式缺陷（进正文返工）+ fixed 取值域/时间锚冲突（v2 锚定纪律：
      当场拒，改正文或总编辑显式改约）；
    - `fact_eventful`：eventful 取值域命中，只记录不挡线；
    - `fact_fixed`：fixed 冲突单独列出，供回执点名（已并入 hard_issues）。
    """
    from .gates import format_defect_issues

    hard = format_defect_issues(prose, chapter=chapter, quote_style=str(cfg.get("quote_style") or "auto"))
    fact_keys = cfg.get("fact_keys") if isinstance(cfg.get("fact_keys"), dict) else {}
    fact_fixed, fact_eventful = declared_fact_conflicts(fact_keys, prose, chapter=chapter)
    anchors = cfg.get("timeline_anchors") if isinstance(cfg.get("timeline_anchors"), dict) else {}
    if anchors:
        age_hits = age_anchor_conflicts(
            prose,
            chapter=chapter,
            chapter_years=anchors.get("chapter_years") if isinstance(anchors.get("chapter_years"), dict) else None,
            birth_years=anchors.get("birth_years") if isinstance(anchors.get("birth_years"), dict) else None,
            tolerance=_as_int(anchors.get("age_tolerance")) or 0,
        )
        age_policy = str(anchors.get("age_policy") or "fixed").strip().lower()
        if age_policy == "eventful":
            fact_eventful.extend(age_hits)
        else:
            fact_fixed.extend(age_hits)
    hard.extend(fact_fixed)
    return hard, fact_eventful, fact_fixed


def run_consistency_audit(store: Any) -> dict[str, Any]:
    """书级事实一致性总入口（只读）。

    汇总：声明表驱动的取值域命中、跨章同键双值、章节格式、跨章近重复、全书高频片段。
    全部题材无关；`fact_keys` / `quant_keys` 留空时默认零输出。
    """
    cfg = store.load_config()
    fact_keys = cfg.get("fact_keys") if isinstance(cfg.get("fact_keys"), dict) else {}
    quant_keys = [str(key) for key in (cfg.get("quant_keys") or []) if str(key).strip()]
    quote_style = str(cfg.get("quote_style") or "auto")
    scan = cfg.get("consistency_scan") if isinstance(cfg.get("consistency_scan"), dict) else {}

    chapters: list[tuple[int, str]] = []
    for path in sorted(store.chapters_dir.glob("*/ch-*.md")):
        try:
            number = int(path.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        chapters.append((number, path.read_text(encoding="utf-8")))

    hits: list[dict[str, Any]] = []
    undeclared: list[dict[str, Any]] = []
    fixed_eventful_split: dict[str, str] = {}
    for key in sorted(fact_keys):
        spec = fact_keys.get(key)
        if not isinstance(spec, dict):
            continue
        fixed_eventful_split[key] = str(spec.get("policy") or "fixed").strip().lower()
        if not _as_list(spec.get("suffixes")) and not str(spec.get("observe") or spec.get("key") or "").strip():
            undeclared.append({"key": key, "reason": "缺少观测模式（observe/key 或 suffixes）"})
            continue
        if spec.get("canonical") is None or str(spec.get("canonical")).strip() == "":
            undeclared.append({"key": key, "reason": "缺少 canonical 规范取值"})
            continue
        for chapter, prose in chapters:
            hits.extend(fact_key_conflicts(prose, spec, key=key, chapter=chapter))

    # 时间锚（出生年 + 故事年 → 年龄算术）：与事实取值域同一纪律，锚未声明恒空。
    anchors = cfg.get("timeline_anchors") if isinstance(cfg.get("timeline_anchors"), dict) else {}
    if anchors:
        for chapter, prose in chapters:
            hits.extend(
                age_anchor_conflicts(
                    prose,
                    chapter=chapter,
                    chapter_years=anchors.get("chapter_years") if isinstance(anchors.get("chapter_years"), dict) else None,
                    birth_years=anchors.get("birth_years") if isinstance(anchors.get("birth_years"), dict) else None,
                    tolerance=_as_int(anchors.get("age_tolerance")) or 0,
                )
            )

    cross = cross_chapter_key_conflicts(chapters, quant_keys)

    format_issues: list[dict[str, Any]] = []
    for chapter, prose in chapters:
        format_issues.extend(
            {"chapter": chapter, **item}
            for item in chapter_format_issues(prose, chapter=chapter, quote_style=quote_style)
        )
    format_issues.extend(chapter_header_style_issues(chapters))

    near = near_duplicate_passages(
        chapters,
        min_hans=int(scan.get("near_duplicate_min_hans") or 60),
        threshold=float(scan.get("near_duplicate_threshold") or 0.85),
    )
    phrases = repeated_phrases(
        chapters,
        min_count=int(scan.get("repeated_phrase_min_count") or 6),
        cap=int(scan.get("repeated_phrase_cap") or 20),
    )

    groups: dict[str, list[int]] = {}
    for hit in hits:
        groups.setdefault(f"{hit['key']}", []).append(int(hit.get("chapter") or 0))
    return {
        "fact_keys": {key: fact_keys[key] for key in sorted(fact_keys)},
        "fact_policies": fixed_eventful_split,
        "timeline_anchors": anchors,
        "hits": hits,
        "weak_declarations": undeclared,
        "cross_chapter": cross,
        "chapter_format": format_issues,
        "near_duplicates": near,
        "repeated_phrases": phrases,
        "summary": {
            "chapters": len(chapters),
            "fact_keys": len(fact_keys),
            "hits": len(hits),
            "keys_with_hits": sorted({hit["key"] for hit in hits}),
            "cross_chapter": len(cross),
            "chapter_format": len(format_issues),
            "near_duplicates": len(near),
            "repeated_phrases": len(phrases),
        },
    }


__all__ = [
    "age_anchor_conflicts",
    "chapter_format_issues",
    "chapter_header_style_issues",
    "count_conflicts",
    "cross_chapter_key_conflicts",
    "date_year_conflicts",
    "declared_fact_conflicts",
    "entity_name_conflicts",
    "entity_suffix_conflicts",
    "fact_key_conflicts",
    "format_and_fact_issues",
    "near_duplicate_passages",
    "number_conflicts",
    "quote_style_issues",
    "repeated_phrases",
    "run_consistency_audit",
    "set_member_conflicts",
]
