"""显性比喻、独白引导词、现代词、职场黑话密度只提供诊断提示；
议论文元叙述标记走硬闸；台词常用口语不入闸。"""

from __future__ import annotations

from novel_ledger_core.content.style_check import check_style_hard


def _metric_names(result: dict) -> list[str]:
    return [item["metric"] for item in result["warnings"]]


def test_simile_saturation_is_advisory() -> None:
    text = (
        "他坐起身，像被人从冷水里捞起来。"
        "门外那团黑影，像一口没底的深井。"
        "手里的火折子晃了晃，像一只将熄的灯蛾。"
        "墙上那道裂缝，就像一条活过来的蜈蚣。"
        "整座院子静得像坟地。"
        "他起身去挑水。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "显性比喻堆叠" in names
    item = next(x for x in result["warnings"] if x["metric"] == "显性比喻堆叠")
    assert item["actual"] >= 4
    assert item["reference_ceiling"] >= 2
    assert item["hits"], "命中清单应带上下文，供文风编辑定向修改"


def test_inner_monologue_label_saturation_is_advisory() -> None:
    text = (
        "他在心里盘算着水缸还有多少水。"
        "他在心里骂了一声。"
        "他在心里问自己该不该去。"
        "他在心里排了排时辰。"
        "他暗想这日子没法过了。"
        "他心道先应下再说。"
        "他低头看了一眼自己的手。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    assert "内心独白引导词" in _metric_names(result)


def test_tech_word_saturation_is_advisory() -> None:
    text = (
        "他像核对报错日志一样翻看前身的记忆。"
        "代码里那种主进程抢占的感觉又来了。"
        "他习惯性地想把问题缓存起来。"
        "屏幕的蓝光在脑子里闪了一下。"
        "架构师的本能让他开始拆解这件事。"
        "他低头挑起水桶，肩上的扁担压得人生疼。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    assert "现代职业词" in _metric_names(result)


def test_single_simile_and_occasional_label_pass_hard_gate() -> None:
    text = (
        "醒过来先闻见一股霉味，潮得发苦，还没睁眼，后背先硌着冷板床，像躺在一排铁条上。"
        "他在心里对自己说：先应下，旁的熬到夜里再想。"
        "他扶着墙挪到门边，拨开门闩。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True, result["fails"]


def test_essay_narrator_markers_fail_hard_gate() -> None:
    """议论文元叙述标记（洞察路标类）与「值得注意的是」同判硬闸。"""
    text = (
        "他把账册合上。需要指出的是，这笔账没人对过。"
        "掌柜的从某种意义上说也只是替人管钱。第二天铺子照常开门。"
    )
    result = check_style_hard(text)
    assert result["ok"] is False
    assert any(item["metric"] == "元叙述/平台话术" for item in result["fails"])


def test_contrast_family_variants_are_advisory() -> None:
    """翻案腔家族变体（并非/不在于/与其说/看似）并入同一 advisory 计数。"""
    text = (
        "他先看了一遍账底，没说话。"
        "这并非毁约，而是改期。"
        "难的不在于写字，而在于谁的名。"
        "与其说是买卖，不如说是换命。"
        "匣子看似寻常，实则夹层里藏着地契。"
        "众人散了。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    names = _metric_names(result)
    assert "AI对照腔 不是A而是B" in names
    item = next(x for x in result["warnings"] if x["metric"] == "AI对照腔 不是A而是B")
    assert item["actual"] == 4
    assert item["limit"] == 1
    assert item["hits"], "对照腔条目应带命中上下文"


def test_jargon_saturation_is_advisory() -> None:
    """职场黑话密度只提示不返工：当代题材台词可能合理使用。"""
    text = (
        "他把这件事当成一次赋能。"
        "两家的买卖彻底闭环。"
        "掌柜的讲的是底层逻辑。"
        "东家只看降本增效。"
        "他低头挑起水桶，肩上的扁担压得人生疼。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True
    assert "职场黑话" in _metric_names(result)


def test_dialogue_legit_colloquialisms_stay_clean() -> None:
    """议论文禁用清单里的口语词在台词/武戏/叙述里自然，不入任何闸。"""
    text = (
        "「说白了，你就是不想去。」他把扁担放下。"
        "拳师打出一套组合拳，收势很稳。"
        "他表面上应了，心里另有打算。"
        "他不只会做饭，还会补衣裳。"
    )
    result = check_style_hard(text)
    assert result["ok"] is True, result["fails"]
    names = _metric_names(result)
    assert "AI对照腔 不是A而是B" not in names
    assert "职场黑话" not in names
