"""市井烟火节奏量测（纯标准库）。

单本小说的分布只适合作写作诊断，不足以判定另一篇小说的单章质量。
逐章硬闸仅拦明显出戏的元叙述、平台话术和明确的章尾套话；句长及其变异、
对白排版、标点、连接词、句式形状（短句排队/同头排比）及词类密度均作 advisory。
返回形状与流水线协议不变。
对照句家族、职场黑话与议论文元叙述标记吸收自议论文 AI 雷区清单：
只收在小说里出戏的条目，标点禁令与台词常用口语不收（小说对白依赖冒号，
词汇面硬禁曾被实测证伪）。句长变异、短句排队与同头排比吸收自同类
议论文机检脚本的形状探针：对白段不参与排队计数，避免误伤快速对攻。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

# 结构层参考带：只给方向提示，不作逐章通过条件。
# 句长CV 校准点（两数据点实测）：AI 样本 ≈66，人写样本 ≈87；
# 下限 70 留了人写自然波动空间，书级可经 style_structure_limits 调整。
_STRUCTURE_SPEC = [
    # (指标, 原书基线, 硬闸下限, 硬闸上限)
    ("句长_中位数", 30.0, 26, 40),
    ("句长_P90", 69.0, 60, 85),
    ("长句占比%(>=50字)", 24.4, 20.0, None),
    ("短句占比%(<=10字)", 13.9, None, 24.0),
    ("句长CV", 87.0, 70.0, None),
]
_STRUCTURE_MIN_CHARS = 2000  # 不足整章的碎片/半成品不做分布判断
_STRUCTURE_HINT = (
    "句长与参考样本有差异；通读本场的停顿和信息密度，必要时调整长短句。"
    "参考带不是逐章目标，不为达标而机械并句。"
)
_STRUCTURE_HINTS = {
    "句长CV": (
        "句长过于均匀，长短句差距接近模型基线；人写的句子该长的长、该短的短。"
        "通读停顿，必要时重排信息密度，不为凑变异硬造句子。"
    ),
}

# 对白排版参考带：只提示检查场面读感，不要求每章对齐原书比例。
_QUOTE_PARA_FLOOR = 10.0
_QUOTE_PARA_MIN_DIALOGUE = 15.0
_QUOTE_PARA_HINT = (
    "对白多埋在叙述段中：通读场面，确认人物的声音和回合仍然清楚；"
    "只有读感确实发闷时，才考虑把适合独立成段的对白拆开。"
)

# 对照句探针：计数只供通读时定位，不按章限制人物说话。
# 家族吸收议论文 AI 翻案腔的常见变体（并非/不在于/与其说/看似）；
# 「不只…还」「表面…实际」在台词与叙事里都自然，不收，避免噪声淹没信号。
_CONTRAST_PATTERNS = [
    re.compile(r"不是[^。！？]{1,30}[，,]\s*(?:而是|是)"),
    re.compile(r"并非[^。！？]{1,30}[，,]\s*而是"),
    re.compile(r"不在于[^。！？]{1,30}[，,]\s*而在于"),
    re.compile(r"与其说[^。！？]{1,30}[，,]\s*不如说"),
    re.compile(r"看似[^。！？]{1,30}[，,]\s*实则"),
]

# (指标, 原书基线, 下限, 上限)
_SPEC = [
    ("句长_中位数", 30.0, 26, 34),
    ("句长_P90", 69.0, 60, 78),
    ("长句占比%(>=50字)", 24.4, 20, None),
    ("短句占比%(<=10字)", 13.9, None, 18),
    ("段长_中位数", 37.0, 32, 42),
    ("对白占比%", 33.1, 28, None),
]
_PUNCT_SPEC = [
    ("，", 64.0, 60, 72),
    ("。", 14.9, 13, 19),
    ("：", 7.8, 6, None),
    ("？", 7.2, 5.5, None),
    ("！", 3.2, 2.2, 4.0),
    ("…", 3.7, 2.5, None),
    ("、", 3.0, 2.0, 4.5),
    ("—", 1.8, 1.0, 2.5),
]
# 硬闸只收对小说叙述出戏的议论文元叙述与平台话术；
# 「说白了/说穿了/先说结论」在口语台词里自然，不进硬闸。
_BANNED = [
    "值得注意的是",
    "需要指出的是",
    "从某种意义上说",
    "综上所述",
    "让我们回到",
    "各位看官",
    "预知后事如何",
    "划重点",
    "家人们",
    "点赞",
    "关注一下",
]
_ADVISORY_TERMS = [
    "众所周知",
    "另一方面",
    "总而言之",
    "令人头皮发麻",
    "倒吸一口凉气",
    "瞳孔骤缩",
    "嘴角勾起一抹弧度",
    "心中一凛",
    "与此同时",
    "紧接着",
]

# 词类密度探针（市井烟火 advisory）。
# 基线全部来自人写网文长篇净化语料实测：
#   显性比喻（，像 / 就好似 / 就像 / 如同 / 仿佛 / 好比）≈0.17/千字，约每 6 章一次；
#   独白引导词（在心里/自问/暗道/暗想/心道/寻思…）≈0.06/千字，约每 7 章一次；
#   现代职业词（代码/日志/屏幕/主进程/架构师…）全书 ≈0。
# 旧阈值保留为诊断参考，不能单凭这些词的出现判文风失败。
_SIMILE_RE = re.compile(
    r"[，。；：！？…、]像|，就好似|，就像|，好比|仿佛|如同"
)
_LABEL_RE = re.compile(
    r"在心里|心里(?:说|骂|问|过|排|立|那|翻|想)|他问自己|他自问|自问道|暗道|暗想|寻思道|心道"
)
_TECH_RE = re.compile(
    r"代码|日志|编程|主进程|屏幕|键盘|电脑|缓存|报错|网页|PPT|Excel|架构师|产品经理|"
    r"需求文档|数据库|算法|编译|操作系统|服务器|显示器|防御性编程|接口|上线|调试"
)

# 职场黑话探针（advisory）：议论文 AI 的抬价词在小说叙述里同样出戏，
# 但当代题材台词可能合理使用，故只计数提示。「组合拳」是正经武学词，
# 「抓手/沉淀/对齐」有常用本义，均不收。
_JARGON_RE = re.compile(
    r"赋能|闭环|底层逻辑|深层逻辑|顶层设计|降本增效|全链路|认知跃迁"
    r"|方法论|颗粒度|生态位|结构性机会"
)

# 网文毒点探针
# 优越感炫耀拆成三个独立模式
_CLICHE_PATTERNS = {
    "巧合堆叠": re.compile(r"正巧|恰好|刚好|偏偏|不巧|碰巧"),
    "情绪夸张": re.compile(r"震惊|骇然|惊呆|目瞪口呆|大吃一惊|瞠目结舌|傻眼"),
    "打脸循环": re.compile(r"你不是说.{1,20}?[吗？！]"),
    "优越感炫耀_殊不知": re.compile(r"殊不知.{5,40}?(?:其实|实际上)"),
    "优越感炫耀_哪里知道": re.compile(r"哪里知道.{5,40}?(?:其实|实际上)"),
    "优越感炫耀_不知道的是": re.compile(r"(?:他们|你们|众人)不知道的是.{5,40}?(?:其实|实际上)"),
}

# 章尾AI套路升华探针（硬红线）
_TAIL_MORALIZING_PATTERNS = re.compile(
    r"(?:未来的路还很长"
    r"|前方的路还很长"
    r"|属于他的传奇"
    r"|命运的齿轮"
    r"|而这一切，?才刚刚开始"
    r"|这一切，?才刚刚拉开序幕"
    r"|这一夜，?注定"
    r"|注定不会平静"
    r"|深知，?前方的路"
    r"|深知，?未来的路"
    r"|在心中默默(?:发誓|下定决心)"
    r"|默默立下誓言"
    r"|新的征程"
    r"|暴风雨前的宁静"
    r"|大幕，?徐徐拉开"
    r"|故事，?才刚刚开始"
    r"|谁又能知道未来"
    r"|究竟会掀起怎样的波澜)"
)


def _clean(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。．！？；…!?;])", text)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _percentile(sorted_vals: list[int], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    if f + 1 >= len(sorted_vals):
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[f + 1] - sorted_vals[f]) * (k - f)


def _sentence_cv(sent_lens: list[int]) -> float:
    """句长变异系数（百分数）。句数不足 12 时返回 0，调用方只对足章判定。"""
    if len(sent_lens) < 12:
        return 0.0
    mean = sum(sent_lens) / len(sent_lens)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in sent_lens) / len(sent_lens)
    return round(100.0 * (variance ** 0.5) / mean, 1)


def _compute_stats(text: str) -> dict[str, Any]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) >= 5]
    sents = _split_sentences(text)
    sent_lens = sorted(len(s) for s in sents)
    para_lens = sorted(len(p) for p in paras)
    dialogue_chars = sum(
        len(m) for m in re.findall(r"[「『“\"](.*?)[」』”\"]", text, flags=re.S)
    )
    puncts = Counter(c for c in text if c in "。．，、！？；：…—！？")
    n = max(len(text), 1)
    return {
        "总字数": len(text),
        "句长_中位数": round(_percentile(sent_lens, 0.5), 1),
        "句长_P90": round(_percentile(sent_lens, 0.9), 1),
        "段长_中位数": round(_percentile(para_lens, 0.5), 1),
        "对白占比%": round(100 * dialogue_chars / n, 1),
        "每千字标点": {k: round(1000 * v / n, 1) for k, v in puncts.most_common()},
        "短句占比%(<=10字)": round(100 * sum(1 for s in sent_lens if s <= 10) / max(len(sent_lens), 1), 1),
        "长句占比%(>=50字)": round(100 * sum(1 for s in sent_lens if s >= 50) / max(len(sent_lens), 1), 1),
        "句长CV": _sentence_cv(sent_lens),
        "段落数": len(paras),
    }


def _verdict(value: float, lo: float | None, hi: float | None) -> str:
    if lo is not None and value < lo:
        return "low"
    if hi is not None and value > hi:
        return "high"
    return "ok"


def _match_contexts(text: str, matcher: re.Pattern[str], limit: int = 8) -> list[str]:
    """命中词前后各取 16 字作上下文，供文风编辑定向修改（不把整章重写成迎合分布）。"""
    return _contexts_from_matches(text, matcher.finditer(text), limit)


def _contexts_from_matches(
    text: str, matches: Iterable[re.Match[str]], limit: int = 8
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        s, e = m.start(), m.end()
        ctx = re.sub(r"\s+", " ", text[max(0, s - 16): e + 16])
        if ctx in seen:
            continue
        seen.add(ctx)
        out.append(ctx)
        if len(out) >= limit:
            break
    return out


def _ai_flavor_fails(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """保留原统计口径作 advisory；单章计数不触发返工。"""
    chars = len(text)
    extra = max(0, (chars - 3000) // 1000)  # 长章每多 1000 字放宽 1
    specs = (
        ("显性比喻堆叠", _SIMILE_RE, 2 + extra, 1),  # (名称, 正则, 历史参考上沿, 提示起点)
        ("内心独白引导词", _LABEL_RE, 3 + extra, 2),
        ("现代职业词", _TECH_RE, 3 + extra, 2),
        ("职场黑话", _JARGON_RE, 2 + extra, 1),
    )
    fails: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for name, matcher, red_line, target in specs:
        hits = list(matcher.finditer(text))
        count = len(hits)
        if count > target:
            warnings.append(
                {
                    "metric": name,
                    "actual": count,
                    "reference_ceiling": red_line,
                    "target": target,
                    "verdict": "high",
                    "hits": _match_contexts(text, matcher),
                    "hint": "计数高于参考样本；结合人物声音和场景判断是否需要修改。",
                }
            )
    return fails, warnings


def check_cliche_saturation(text: str) -> dict[str, Any]:
    """检测网文毒点饱和度（巧合堆叠/情绪夸张/打脸循环/优越感炫耀）。

    阈值基于人写网文长篇全量统计（待补充实测数据）。
    当前使用保守阈值作为初版红线。

    返回：
    - ok: 是否通过
    - fails: 超过阈值的项
    - details: 每个类别的详细统计
    """
    char_count = len(re.sub(r"\s", "", text))
    results: dict[str, Any] = {}

    # 优越感炫耀需要合并三个子模式
    superiority_count = 0
    superiority_hits: list[str] = []

    for name, pattern in _CLICHE_PATTERNS.items():
        matches = list(pattern.finditer(text))
        hits_list = [m.group() for m in matches]

        if name.startswith("优越感炫耀_"):
            superiority_count += len(matches)
            superiority_hits.extend(hits_list[:2])
            continue

        density = len(matches) / (char_count / 1000) if char_count > 0 else 0
        results[name] = {
            "count": len(matches),
            "density_per_k": round(density, 2),
            "hits": hits_list[:5],
        }

    # 合并优越感炫耀的统计
    results["优越感炫耀"] = {
        "count": superiority_count,
        "density_per_k": round(superiority_count / (char_count / 1000), 2) if char_count > 0 else 0,
        "hits": superiority_hits[:5],
    }

    # 设定阈值（基于原著统计，当前使用保守估计）
    fails: list[dict[str, Any]] = []
    thresholds = {
        "巧合堆叠": 2,
        "情绪夸张": 3,
        "打脸循环": 2,
        "优越感炫耀": 1,
    }

    for name, threshold in thresholds.items():
        if results[name]["count"] > threshold:
            fails.append({
                "metric": name,
                "actual": results[name]["count"],
                "limit": threshold,
                "hits": results[name]["hits"],
            })

    return {"ok": len(fails) == 0, "fails": fails, "details": results}


def check_tail_moralizing(text: str) -> dict[str, Any]:
    """检测章节末尾是否存在 AI 套路化抒情升华或鸡汤总结（硬红线）。

    网文断章艺术重在悬念留白、动作定格与即时危机，严禁在末尾写小学作文式总结
    （如‘未来的路还很长’、‘命运的齿轮’、‘属于他的传奇’）。
    """
    if not text or not isinstance(text, str):
        return {"ok": True, "fails": []}
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    if not paras:
        return {"ok": True, "fails": []}
    # 取最后 2 段，合并检查末尾 400 字
    tail_text = "\n".join(paras[-2:])[-400:]
    matches = list(_TAIL_MORALIZING_PATTERNS.finditer(tail_text))
    if not matches:
        return {"ok": True, "fails": []}

    hits = [m.group(0) for m in matches]
    fails = [{
        "metric": "章尾AI升华与套话总结",
        "actual": len(hits),
        "limit": 0,
        "hits": hits,
        "hint": f"章节末尾出现套路化AI升华/抒情总结（{'/'.join(hits)}）！网文结尾严禁总结陈词，必须动作定格或悬停留白，请打回重润。",
    }]
    return {"ok": False, "fails": fails}


def _merged_structure_spec(limits: dict[str, Any] | None) -> list[tuple[str, float, float | None, float | None]]:
    """按 config.style_structure_limits 覆盖默认硬闸限值（配额可按书按阶段调整）。

    只认 _STRUCTURE_SPEC 里的指标名。值语义：
    - 裸数字：默认带上限的指标（短句占比）替换**上限**，其余替换**下限**；
    - [lo, hi] 二元组：两边同改（单边可传 None 保持默认）。
    漏给的指标用默认。
    """
    if not isinstance(limits, dict) or not limits:
        return _STRUCTURE_SPEC
    merged: list[tuple[str, float, float | None, float | None]] = []
    for name, base, lo, hi in _STRUCTURE_SPEC:
        value = limits.get(name)
        new_lo, new_hi = lo, hi
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is None:
                new_hi = value
            else:
                new_lo = value
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            v_lo, v_hi = value
            if isinstance(v_lo, (int, float)) and not isinstance(v_lo, bool):
                new_lo = v_lo
            if isinstance(v_hi, (int, float)) and not isinstance(v_hi, bool):
                new_hi = v_hi
        merged.append((name, base, new_lo, new_hi))
    return merged


def _quote_para_fails(text: str) -> list[dict[str, Any]]:
    """对白段型参考带的偏离项；调用方将其作为 advisory。"""
    stats = _compute_stats(text)
    if stats["总字数"] < _STRUCTURE_MIN_CHARS:
        return []
    dialogue = stats["对白占比%"]
    if dialogue < _QUOTE_PARA_MIN_DIALOGUE:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) >= 5]
    if not paras:
        return []
    quoted = 100.0 * sum(1 for p in paras if p[0] in "“「『\"") / len(paras)
    if quoted >= _QUOTE_PARA_FLOOR:
        return []
    return [
        {
            "metric": "引号开头段落%",
            "base": 18.7,
            "actual": round(quoted, 1),
            "verdict": "low",
            "limit": _QUOTE_PARA_FLOOR,
            "hint": _QUOTE_PARA_HINT,
        }
    ]


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")

# 短句排队：连续 ≥4 个极短叙述段（汉字≤24 且单句）。对白段天然就短，
# 不计入也不打断连排——只有叙述本身连成一梭子才提示。
_SHORT_PARA_HAN = 24
_SHORT_PARA_STREAK = 4

# 同头排比：一句话里 ≥3 个小句用同一个两字开头。
_ANAPHORA_MIN_RUN = 3


def _short_para_streak(text: str) -> list[dict[str, Any]]:
    """连续极短叙述段的排队形状；调用方将其作为 advisory。"""
    if len(text) < _STRUCTURE_MIN_CHARS:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    streak = best = 0
    for para in paras:
        if para[0] in "“「『\"":
            streak = 0  # 对白回合是自然短段，重排计数
            continue
        han = len(_HAN_RE.findall(para))
        sentences = len(re.findall(r"[。！？!?]", para))
        if han <= _SHORT_PARA_HAN and sentences <= 1:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    if best < _SHORT_PARA_STREAK:
        return []
    return [
        {
            "metric": "短句排队",
            "base": 0,
            "actual": best,
            "verdict": "high",
            "limit": _SHORT_PARA_STREAK - 1,
            "hint": "连续多个极短叙述段连成一排；人写的短句插在长句之间。"
            "通读节奏，合并或充实其中段落；对白段不在此列。",
        }
    ]


def _anaphora_runs(text: str) -> list[dict[str, Any]]:
    """同句内多个小句同头的排比形状；调用方将其作为 advisory。"""
    if len(text) < _STRUCTURE_MIN_CHARS:
        return []
    flagged: list[re.Match[str]] = []
    for sentence in re.finditer(r"[^。！？!?\n]+(?:[。！？!?]|$)", text):
        clauses = [
            c.strip()
            for c in re.split(r"[，、；,;]", sentence.group())
            if len(_HAN_RE.findall(c)) >= 3
        ]
        run = 1
        for prev, curr in zip(clauses, clauses[1:]):
            if prev[:2] == curr[:2] and re.match(r"[\u4e00-\u9fff]{2}", curr):
                run += 1
                if run >= _ANAPHORA_MIN_RUN:
                    flagged.append(sentence)
                    break
            else:
                run = 1
    if not flagged:
        return []
    return [
        {
            "metric": "同头排比",
            "base": 0,
            "actual": len(flagged),
            "verdict": "high",
            "limit": 0,
            "hits": _contexts_from_matches(text, flagged),
            "hint": "同句内多个小句用同一个开头；除非是人物刻意的修辞，"
            "否则变换句首。词语本身不构成错误。",
        }
    ]


def _structure_fails(
    text: str,
    *,
    structure_off: bool = False,
    structure_limits: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """句长参考带偏离项。只对足章文本生效，避免碎片自查误判分布。

    `structure_off=True`（config.style_structure="off"）关闭这组提示。
    """
    if structure_off:
        return []
    stats = _compute_stats(text)
    if stats["总字数"] < _STRUCTURE_MIN_CHARS:
        return []
    fails: list[dict[str, Any]] = []
    for name, base, lo, hi in _merged_structure_spec(structure_limits):
        verdict = _verdict(stats[name], lo, hi)
        if verdict != "ok":
            fails.append(
                {
                    "metric": name,
                    "base": base,
                    "actual": stats[name],
                    "verdict": verdict,
                    "limit": lo if verdict == "low" else hi,
                    "hint": _STRUCTURE_HINTS.get(name, _STRUCTURE_HINT),
                }
            )
    return fails


def _contrast_fails(text: str, *, contrast_limit: int = 1) -> list[dict[str, Any]]:
    """对照句计数偏离项；调用方将其作为 advisory。翻案腔家族各变体合并计数。"""
    extra = max(0, (len(text) - 3000) // 1000)
    red_line = max(0, int(contrast_limit)) + extra
    matches = [m for p in _CONTRAST_PATTERNS for m in p.finditer(text)]
    if len(matches) <= red_line:
        return []
    return [
        {
            "metric": "AI对照腔 不是A而是B",
            "base": 0,
            "actual": len(matches),
            "verdict": "high",
            "limit": red_line,
            "hits": _contexts_from_matches(text, matches),
            "hint": "对照句出现较多；结合人物说话习惯与场景判断是否重复，不按固定次数删句。",
        }
    ]


def _hard_fails(
    text: str,
    *,
    structure_off: bool = False,
    structure_limits: dict[str, Any] | None = None,
    contrast_limit: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐章只拦明确出戏的套话；原作统计与风格偏差作为 warnings。"""
    fails: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    banned_hits = [w for w in _BANNED if w in text]
    if banned_hits:
        fails.append(
            {
                "metric": "元叙述/平台话术",
                "base": 0,
                "actual": len(banned_hits),
                "verdict": "high",
                "hits": banned_hits,
            }
        )
    advisory_counts = {w: text.count(w) for w in _ADVISORY_TERMS if w in text}
    advisory_total = sum(advisory_counts.values())
    if advisory_total >= 3 or any(count >= 2 for count in advisory_counts.values()):
        warnings.append({
            "metric": "常见连接词/套语",
            "actual": advisory_total,
            "verdict": "high",
            "hits": list(advisory_counts),
            "hint": "同章出现重复表达；核对节奏与语境，词语本身不构成错误。",
        })
    semi = text.count("；")
    if semi >= 3:
        warnings.append({
            "metric": "分号",
            "actual": semi,
            "verdict": "high",
            "hint": "同章分号较密；检查停顿是否自然，分号本身不构成错误。",
        })

    _, ai_warnings = _ai_flavor_fails(text)
    warnings.extend(ai_warnings)

    # 套路词密度仅供人工通读时定位，不能以词面计数判正文失败。
    cliche_result = check_cliche_saturation(text)
    if not cliche_result["ok"]:
        warnings.extend({**item, "verdict": "high"} for item in cliche_result["fails"])

    # 章尾AI套话升华检测（硬红线）
    tail_result = check_tail_moralizing(text)
    if not tail_result["ok"]:
        fails.extend(tail_result["fails"])

    # 原书的句长、对白排版与对照句数量只作诊断。
    warnings.extend(
        _structure_fails(text, structure_off=structure_off, structure_limits=structure_limits)
    )
    if not structure_off:
        warnings.extend(_quote_para_fails(text))
        warnings.extend(_short_para_streak(text))
        warnings.extend(_anaphora_runs(text))
    warnings.extend(_contrast_fails(text, contrast_limit=contrast_limit))

    return fails, warnings


def _distribution_fails(text: str) -> list[dict[str, Any]]:
    """节奏分布画像（句长/对白/段长/标点/段落层）。"""
    s = _compute_stats(text)
    fails: list[dict[str, Any]] = []

    def check(name: str, base: float, value: float, lo: float | None, hi: float | None) -> None:
        v = _verdict(value, lo, hi)
        if v != "ok":
            fails.append({"metric": name, "base": base, "actual": value, "verdict": v})

    for name, base, lo, hi in _SPEC:
        check(name, base, s[name], lo, hi)
    for punct, base, lo, hi in _PUNCT_SPEC:
        check(f"标点{punct}", base, s["每千字标点"].get(punct, 0.0), lo, hi)

    turns = [x for x in re.findall(r"[“「](.*?)[”」]", text, flags=re.S) if x.strip()]
    if turns:
        lengths = sorted(len(x) for x in turns)
        avg = round(sum(lengths) / len(lengths), 1)
        short = round(100 * sum(1 for x in lengths if x <= 6) / len(lengths), 1)
        long_ = round(100 * sum(1 for x in lengths if x >= 30) / len(lengths), 1)
        for name, base, value, lo, hi in [
            ("对白平均字数", 27.5, avg, 22, None),
            ("对白≤6字占比%", 20.0, short, None, 30),
            ("对白≥30字占比%", 31.1, long_, 25, None),
        ]:
            check(name, base, value, lo, hi)

    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) >= 5]
    if paras:
        num = re.compile(r"[一二三四五六七八九十百千万零两0-9]")
        numq = re.compile(
            r"[一二三四五六七八九十百千万两0-9]\s*"
            r"[块两个道张条件层刻里步文寸尺丈碗副次回把只面枚份年日月时天人钱袋]"
        )
        quoted = sum(1 for p in paras if p[0] in "“「")
        check("段落≤80字%", 89.0, round(100 * sum(1 for p in paras if len(p) <= 80) / len(paras), 1), 85, 93)
        check("含数目字段落%", 60.6, round(100 * sum(1 for p in paras if num.search(p)) / len(paras), 1), 55, 70)
        check("数+量段落%", 29.7, round(100 * sum(1 for p in paras if numq.search(p)) / len(paras), 1), 25, 38)
        check("引号开头段落%", 18.7, round(100 * quoted / len(paras), 1), None, 24)

    return fails


def _first_title_stripped(raw: str) -> str:
    return re.sub(r"^第[^\n]{1,20}\n+", "", _clean(raw), flags=re.M)


def _ai_quota_targets(text: str) -> dict[str, dict[str, int]]:
    """保留旧计数带供既有消费者展示；这些数值不再触发硬闸。"""
    extra = max(0, (len(text) - 3000) // 1000)
    return {
        "显性比喻堆叠": {"red_line": 2 + extra, "target": 1},
        "内心独白引导词": {"red_line": 3 + extra, "target": 2},
        "现代职业词": {"red_line": 3 + extra, "target": 2},
    }


def check_style_hard(
    raw: str,
    *,
    structure_off: bool = False,
    structure_limits: dict[str, Any] | None = None,
    contrast_limit: int = 1,
) -> dict[str, Any]:
    """逐章写作期闸门：明确出戏的套话为 hard，统计偏离为 advisory。

    结构层三个参数由调用方从书级 config 传入（pipeline._style_structure_args）：
    - structure_off：关闭句长与对白段型提示；
    - structure_limits：调整句长提示的参考带；
    - contrast_limit：调整对照句提示的参考计数。

    返回字典包含：
    - ok: 是否通过硬红线
    - fails: 未达硬红线的项（触发返工）
    - warnings: 统计分布与常见表达的提示（不触发返工）
    - targets: 历史参考计数，非逐章达标要求
    """
    text = _first_title_stripped(raw)
    fails, warnings = _hard_fails(
        text,
        structure_off=structure_off,
        structure_limits=structure_limits,
        contrast_limit=contrast_limit,
    )

    return {
        "ok": len(fails) == 0,
        "fails": fails,
        "warnings": warnings,
        "targets": _ai_quota_targets(text),
    }


def style_direction(raw: str) -> list[dict[str, Any]]:
    """单章节奏分布方向（只提示，不挡流水线）。"""
    return _distribution_fails(_first_title_stripped(raw))


def check_style_text(
    raw: str,
    *,
    structure_off: bool = False,
    structure_limits: dict[str, Any] | None = None,
    contrast_limit: int = 1,
) -> dict[str, Any]:
    """完整节奏指纹：批量/卷末累计层使用。结构层参数与
    check_style_hard 同源（书级 config），但统计偏离只作诊断。

    返回 {ok, fails, hard_fails, distribution_fails, warnings, targets}。
    ok/fails 只表示明确出戏的硬问题；distribution_fails 是历史字段名，
    其中的统计偏离只供阅读诊断。"""
    text = _first_title_stripped(raw)
    hard_fails, warnings = _hard_fails(
        text,
        structure_off=structure_off,
        structure_limits=structure_limits,
        contrast_limit=contrast_limit,
    )
    dist = _distribution_fails(text)
    fails = hard_fails

    return {
        "ok": not fails,
        "fails": fails,
        "hard_fails": hard_fails,
        "distribution_fails": dist,
        "warnings": warnings,
        "targets": _ai_quota_targets(text),
    }


# —— 日志体开场探针 ——
# 现场形态（长跑自然收敛）：简报/正典灌满绝对日期 + 机检盯时间线，写者的安全牌
# 变成每章以「X月X号早上X点」开场——连续数十章同款日志句，读感从小说滑向工作日志。
# 只判「章首句是日期/时刻公式」这一件事：单章是 NIT（正文合法需求存在），连续才升级。
_DATELINE_NUMBER = r"[一二两三四五六七八九十百零〇\d]{1,4}"
_DATELINE_OPENING_RE = re.compile(
    r"^(?:[一二八九〇零\d]{2,4}年)?" + _DATELINE_NUMBER + r"月" + _DATELINE_NUMBER + r"[号日]"
    r"|^(?:早上|上午|下午|傍晚|晚上|清晨|凌晨)" + _DATELINE_NUMBER + r"点"
)


def dateline_opening(text: str) -> dict[str, Any] | None:
    """章首句是否为日志式日期/时刻公式（如「三月五号，早上七点五十。」/「下午两点半，…」）。

    只看首个非空行的开头；裸时刻（「十一点过一刻」无上午/下午前缀）不算——
    它是场景内时间，不是日历句。返回 None 表示正常开场。
    """
    cleaned = _clean(text)
    if not cleaned:
        return None
    first_line = cleaned.split("\n", 1)[0].strip()
    if not first_line:
        return None
    if _DATELINE_OPENING_RE.match(first_line):
        return {"code": "dateline_opening", "opening": first_line[:24]}
    return None
