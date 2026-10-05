"""市井烟火结构分布为诊断提示；节奏锚切片保持确定性。"""

from __future__ import annotations

from pathlib import Path

from novel_ledger_core.content.style_check import check_style_hard
from novel_ledger_core.content.views import make_polish_view
from novel_ledger_core.voice.anchor import (
    ANCHOR_HEADER,
    build_voice_anchor,
    load_anchor_source,
    select_anchor_slices,
)


def _metric_names(result: dict) -> list[str]:
    return [item["metric"] for item in result["warnings"]]


def _choppy_chapter() -> str:
    """碎句化整章（>2000 字）：每句 7–14 字，无长流水句——AI 均匀节奏的极端形态。"""
    sentence = "他把账本翻过一页。茶碗在案上放着。外头有人喊了一嗓子。"
    paragraph = "殿里静了下来。他没接话。这事就先搁下。\n"
    return sentence * 30 + "\n" + paragraph * 90


def test_choppy_prose_warns_without_blocking() -> None:
    result = check_style_hard(_choppy_chapter())
    assert result["ok"] is True
    names = _metric_names(result)
    assert "句长_中位数" in names
    assert "句长_P90" in names
    assert "长句占比%(>=50字)" in names
    item = next(x for x in result["warnings"] if x["metric"] == "句长_中位数")
    assert item["actual"] < item["limit"]
    assert item["hint"], "结构条目必须带修法提示，否则文风编辑只能猜"


def test_source_style_punctuation_and_connectors_do_not_block() -> None:
    text = _choppy_chapter() + (
        "\n与此同时，掌柜翻了一页账；紧接着又问价。"
        "与此同时，伙计把碗端来；掌柜又拿起笔；"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    assert result["fails"] == []
    names = _metric_names(result)
    assert {"分号", "常见连接词/套语", "句长_中位数"} <= set(names)


def test_occasional_semicolon_and_connector_need_no_warning() -> None:
    text = "与此同时，掌柜翻了一页账；紧接着，伙计端来一碗粥；"
    result = check_style_hard(text)
    assert result["ok"] is True
    assert "分号" not in _metric_names(result)
    assert "常见连接词/套语" not in _metric_names(result)


_S60 = (
    "他把十七家的账底挨个翻过一遍，粗瓷碗沿上结着经年的茶垢，续水的小道童从殿东头"
    "跑到殿西头，山风还顺着门槛缝往里钻，案角的黄麻纸被吹得哗啦翻卷，没人腾出手去压。"
)
_S34 = "殿里静了一瞬，桂执事把茶盏搁回案上，没再说话，底下有人跟着翻自家册子。"
_S15 = "殿里静了一瞬，桂执事没再说话。"
_S10 = "茶凉了。"


def _bimodal_chapter() -> str:
    """长短双峰落进人写带（中位 26–40）：长链为主 + 中短句收力，~2200 字。"""
    return _S60 * 15 + _S34 * 30 + _S10 * 12


def test_bimodal_prose_passes_without_structure_warning() -> None:
    result = check_style_hard(_bimodal_chapter())
    assert result["ok"] is True, result["fails"]


def test_short_sentence_bias_is_diagnostic() -> None:
    text = _S60 * 12 + _S15 * 50 + _S34 * 10 + _S10 * 40  # 中位 ~15、长句 ~11%
    names = _metric_names(check_style_hard(text))
    assert "句长_中位数" in names and "长句占比%(>=50字)" in names


def test_inline_dialogue_chapter_warns_without_blocking() -> None:
    narration = _S60 * 22
    # 对白全部嵌在段落中部，段落一律以叙述起头；对白占比 ~24%
    embedded = (
        "桂执事扫了一圈才开口，他道：“账目底，各家今天带齐了没有？缺一家，这约就先不立，"
        "立约是把家底摊开当众对账，谁也别想含糊过去，带不齐的现在说，出门去取。”底下没人应声。"
    ) * 14
    text = narration + "\n" + embedded
    result = check_style_hard(text)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "引号开头段落%" in names
    item = next(x for x in result["warnings"] if x["metric"] == "引号开头段落%")
    assert item["actual"] < item["limit"]
    assert "对白" in item["hint"]


def test_quote_led_dialogue_chapter_needs_no_quote_warning() -> None:
    narration = _S60 * 30
    dialogue = "“账目底，各家今天带齐了没有？缺一家这约就先不立。”\n“齐了。”\n“齐了就开收。”\n" * 30
    text = narration + "\n" + dialogue
    assert "引号开头段落%" not in _metric_names(check_style_hard(text))


def test_fragment_below_chapter_size_skips_structure_advisory() -> None:
    """不足 2000 字的碎片/半成品不做分布判断（预检片段不误杀）。"""
    result = check_style_hard("他坐起来。天没亮。水缸是空的。他叹了口气。" * 5)
    assert "句长_中位数" not in _metric_names(result)


def test_contrast_flip_count_is_advisory() -> None:
    one = "立约不是喝彩，是把家底摊在一块儿的事。"
    text = (
        "他先看了一遍账底，没说话。"
        + one * 3
        + "众人把匣子装好，散了。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    assert "AI对照腔 不是A而是B" in _metric_names(result)
    item = next(x for x in result["warnings"] if x["metric"] == "AI对照腔 不是A而是B")
    assert item["limit"] == 1
    assert item["hits"], "对照腔条目应带命中上下文"


def test_single_contrast_flip_passes() -> None:
    text = (
        "他先看了一遍账底，没说话。"
        "立约不是喝彩，是把家底摊在一块儿的事。"
        "众人把匣子装好，散了。"
    )
    result = check_style_hard(text)
    assert "AI对照腔 不是A而是B" not in _metric_names(result)


# ── 书级调节旋钮（config：style_structure / style_structure_limits / style_contrast_limit）──


def test_structure_advisories_can_be_turned_off() -> None:
    result = check_style_hard(_choppy_chapter(), structure_off=True)
    assert "句长_中位数" not in _metric_names(result)
    assert "长句占比%(>=50字)" not in _metric_names(result)


def test_structure_limits_override_per_metric() -> None:
    # 放宽句长中位数到 6（碎句稿实测中位 7.0）：中位数项放行，其余指标照拦
    result = check_style_hard(
        _choppy_chapter(), structure_limits={"句长_中位数": 6}
    )
    names = _metric_names(result)
    assert "句长_中位数" not in names
    assert "句长_P90" in names
    # 收紧下限到 36（双峰样例中位 ~34）：双峰样例也开始不过中位数
    bimodal = _bimodal_chapter()
    assert "句长_中位数" in _metric_names(
        check_style_hard(bimodal, structure_limits={"句长_中位数": [36, 40]})
    )


def test_contrast_limit_is_configurable() -> None:
    text = (
        "他先看了一遍账底，没说话。"
        "立约不是喝彩，是把家底摊在一块儿的事。"
        "这不是排座次，是议事。"
        "众人把匣子装好，散了。"
    )
    # 默认配额 1：两处翻转 → 不过
    assert "AI对照腔 不是A而是B" in _metric_names(check_style_hard(text))
    # 放宽到 2 → 过
    assert "AI对照腔 不是A而是B" not in _metric_names(
        check_style_hard(text, contrast_limit=2)
    )


# ── 句式形状探针（句长CV / 短句排队 / 同头排比，吸收自议论文机检的形状家族）──


def _filler_chapter(repeats: int = 60) -> str:
    """足章叙述填充段：句子长短交错但都绕开各类探针。"""
    sentence = "他先把井绳解开，把桶沉下去，等水灌满，再一把一把提上来，倒在石台边的缸里。"
    return sentence * repeats


def test_uniform_sentence_length_low_cv_is_advisory() -> None:
    """等长句整章：句长变异趋近 0，低 burstiness 的极端形态。"""
    sentence = "他把水桶提上井台，又把绳子绕了两圈，看看天色还早。"
    text = (sentence * 8 + "\n\n") * 12
    result = check_style_hard(text)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "句长CV" in names
    item = next(x for x in result["warnings"] if x["metric"] == "句长CV")
    assert item["verdict"] == "low"
    assert item["limit"] == 70


def test_bimodal_sentence_length_passes_cv() -> None:
    """长短交替（4 字短句 × 50+ 字长句）：CV 必然远高于下限，不提示。"""
    long_sent = (
        "他把这件事从头到尾想了一遍"
        "，再把各人的话都想了一遍"
        "，又把各人的账都算了一遍"
    )
    text = ("刀落了。" + long_sent) * 40
    result = check_style_hard(text)
    assert "句长CV" not in _metric_names(result)


def test_short_para_streak_is_advisory() -> None:
    """连续极短叙述段连成一排才提示；对白回合重置计数。"""
    streak_paras = "\n\n".join(
        ["天还没亮。", "水缸是空的。", "他没说话。", "锅也是冷的。", "他先出了门。"]
    )
    result = check_style_hard(_filler_chapter() + "\n\n" + streak_paras)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "短句排队" in names
    item = next(x for x in result["warnings"] if x["metric"] == "短句排队")
    assert item["actual"] == 5
    assert item["limit"] == 3


def test_dialogue_turns_break_short_para_streak() -> None:
    """对白段天然就短：短叙述与对白交错不构成排队。"""
    interleaved = "\n\n".join(
        ["天还没亮。", "“掌柜的还没起？”", "水缸是空的。", "“先挑水去。”", "他没说话。"]
    )
    result = check_style_hard(_filler_chapter() + "\n\n" + interleaved)
    assert "短句排队" not in _metric_names(result)


def test_anaphora_run_is_advisory() -> None:
    """同句内三个小句同两字开头才提示；两连不算。"""
    flagged = "他不敢看账，他不敢问人，他不敢想往后，只能先把门关上。"
    pair_only = "他不敢看，他不敢问，最后还是把账翻开了。"
    result = check_style_hard(_filler_chapter() + flagged + pair_only)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "同头排比" in names
    item = next(x for x in result["warnings"] if x["metric"] == "同头排比")
    assert item["actual"] == 1
    assert item["hits"], "排比条目应带命中上下文"
    # 两连开头不触发
    result_pair = check_style_hard(_filler_chapter() + pair_only)
    assert "同头排比" not in _metric_names(result_pair)


# ── 节奏锚切片 ──────────────────────────────────────────────────────────────


def _anchor_source() -> str:
    return "\n".join(f"第{i}段。他往灶膛里添了一把柴，火苗舔着锅底，水汽顺锅盖边冒出来，" for i in range(400))


def test_anchor_slices_are_deterministic_per_chapter() -> None:
    src = _anchor_source()
    a1 = select_anchor_slices(src, chapter=7, chars=800, slices=2)
    a2 = select_anchor_slices(src, chapter=7, chars=800, slices=2)
    assert a1 == a2, "同一章重建 pack 必须得到同一批切片（保指纹与 prompt cache）"
    b = select_anchor_slices(src, chapter=8, chars=800, slices=2)
    assert a1 != b, "章号滚动应覆盖语料不同位置"


def test_anchor_slices_stay_on_paragraph_boundaries_and_capped() -> None:
    src = _anchor_source()
    out = select_anchor_slices(src, chapter=3, chars=600, slices=2)
    body = out.split(ANCHOR_HEADER, 1)[1]
    paras = [p for p in body.split("\n") if p.strip() and p.strip() != "……"]
    assert all(p.startswith("第") for p in paras), "切片必须落在整段落上，不截半句"
    assert sum(len(p) for p in paras) <= 900, "合计约 chars 字，允许少量溢出但不翻倍"


def test_anchor_header_declares_no_content_leak() -> None:
    out = select_anchor_slices(_anchor_source(), chapter=1, chars=400, slices=1)
    assert "严禁借用" in out.split("\n")[0], "切片头部必须自带内容防泄漏声明"


def test_build_voice_anchor_reads_gb18030_and_honors_config(tmp_path: Path) -> None:
    ref = tmp_path / "ref.txt"
    ref.write_text(_anchor_source() * 3, encoding="gb18030")
    cfg = {"voice_anchor_file": str(ref), "voice_anchor_chars": 500, "voice_anchor_slices": 1}
    out = build_voice_anchor(tmp_path, cfg, chapter=2)
    assert out.startswith(ANCHOR_HEADER)
    # 相对路径按项目根解析
    cfg_rel = {"voice_anchor_file": "ref.txt"}
    assert build_voice_anchor(tmp_path, cfg_rel, chapter=2)
    # 未配置 / 文件缺失 → 空串，不炸流水线
    assert build_voice_anchor(tmp_path, {}, chapter=2) == ""
    assert build_voice_anchor(tmp_path, {"voice_anchor_file": "nope.txt"}, chapter=2) == ""
    # 章标题行在清洗时剥掉
    titled = tmp_path / "titled.txt"
    titled.write_text("第十二章 旧事\n" + _anchor_source(), encoding="utf-8")
    cleaned = load_anchor_source(titled)
    assert "第十二章" not in cleaned


def test_polish_view_carries_anchor_when_pack_has_it() -> None:
    pack = {
        "chapter": 3,
        "voice_writing_text": "手册内文",
        "voice_anchor_text": ANCHOR_HEADER + "\n\n锚段正文",
    }
    view = make_polish_view(pack)
    assert view["voice_anchor_text"] == pack["voice_anchor_text"]
    # 未配置锚的 pack 不产生空字段
    bare = make_polish_view({"chapter": 3, "voice_writing_text": "手册内文"})
    assert "voice_anchor_text" not in bare
