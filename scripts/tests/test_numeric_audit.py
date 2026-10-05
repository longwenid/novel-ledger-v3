"""跨章数字巡检内核：枚举求和、同键双值、派生件数值脱锚。

这些探针补上字数/标点/术语/连续性机检都看不见的一类漂移：正文里可一算就穿的**数**。
全部题材无关，口径词只从项目配置（quant_keys/glossary）与正文取；默认配置下零输出。
"""

from __future__ import annotations

from novel_ledger_core.content.numeric_audit import (
    algebraic_flow_mismatches,
    derived_numeric_drift,
    enum_sum_mismatches,
    parse_number,
    same_key_conflicts,
)


def test_parse_number_handles_colloquial_omission():
    assert parse_number("一百二十") == 120
    assert parse_number("三百二") == 320  # 口语省略：末尾数字按上一单位 1/10
    assert parse_number("二百四") == 240
    assert parse_number("一千零八十") == 1080
    assert parse_number("两") == 2
    assert parse_number("十五") == 15
    assert parse_number("不是数字") is None


def test_explicit_zero_keeps_the_terminal_digit_in_its_actual_place():
    for token, expected in {
        "零": 0, "三百零二": 302, "三百〇二": 302,
        "一千零五": 1005, "一万零一": 10001,
        "一万零一百零二": 10102, "一万二千零五": 12005,
        "三百二": 320, "一千五": 1500, "一万二": 12000,
        "一千零八十": 1080, "一万零二千": 12000,
    }.items():
        assert parse_number(token) == expected, token


def test_asset_flow_uses_explicit_zero_without_false_pass_or_false_alarm():
    assert algebraic_flow_mismatches("原有三百零二文，付了两文，还剩三百文。") == []
    wrong = algebraic_flow_mismatches("原有三百零二文，付了两文，还剩三百一十八文。")
    assert len(wrong) == 1
    assert wrong[0]["computed_remain"] == 300
    assert wrong[0]["stated_remain"] == 318


def test_enum_sum_mismatch_catches_wrong_total():
    # 120+240+36+120=516，正文却写拢共三百九十六（ch41 当年的硬伤）
    prose = "铺面月租一百二十文、押两个月二百四十文、中人钱按一成五三十六文、验单一百二十文，拢共三百九十六文。"
    hits = enum_sum_mismatches(prose)
    assert hits and hits[0]["computed"] == 516 and hits[0]["stated"] == 396


def test_enum_sum_ignores_ratio_prefix_and_shared_unit():
    # 「一成五」是比例不是金额；「二百与三百文」是共用单位的两个加数
    ok = "验单一趟坐底两贯，下地窖另加一贯，加急添三百，何记照行规抽一成五，拢共三贯三百文。"
    assert enum_sum_mismatches(ok) == []
    shared = "护院符牌一趟二百与三百文，拢共五百文。"
    assert enum_sum_mismatches(shared) == []


def test_enum_sum_requires_two_addends():
    assert enum_sum_mismatches("他欠了三百文，拢共三百文。") == []


def test_same_key_conflict_is_opt_in_and_requires_money_unit():
    prose = "月租一百二十文。后来月租涨到二百文。"
    # 未声明键 → 恒空（默认零误报）
    assert same_key_conflicts(prose, []) == []
    hits = same_key_conflicts(prose, ["月租"])
    assert hits and hits[0]["values"] == [120, 200]


def test_derived_numeric_drift_flags_old_value_still_in_summary():
    # 正文已改尺为四百四十方，摘要仍停在旧值二百方
    prose = "依母符压。四百四十方。工钱六百文。"
    drift = derived_numeric_drift(prose, {"meta.l1_summary": "账底一行“依母符压、二百方”"})
    assert drift and drift[0]["missing"] == ["二百方"]


def test_derived_numeric_drift_tolerates_paraphrase_same_value():
    # 摘要把「二百与三百文」改写成「二百文」：数词仍在正文里，不该报
    prose = "护院符牌一趟二百与三百文。"
    assert derived_numeric_drift(prose, {"summaries.l1": "两户护院符牌二百文"}) == []


def test_propose_quant_keys_from_canon_cards():
    from novel_ledger_core.content.numeric_audit import propose_quant_keys

    cards = [
        {"id": "量化口径--硬-符阵行工钱-承-符阵修补", "body": "- 验阵：**两三贯起**；牙行抽一成五。"},
        {"id": "量化口径--硬-灵田租额与地价", "body": "- 年租按田等：上田一百五十文。"},
        {"id": "别的卡--硬", "body": "**不该出现**"},
    ]
    out = propose_quant_keys(cards)
    assert out["candidates"] == ["符阵行工钱", "灵田租额与地价"]
    assert "两三贯起" in out["bold_terms"]
    assert "不该出现" not in out["bold_terms"]
