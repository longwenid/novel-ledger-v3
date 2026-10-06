"""书级事实一致性内核：取值域判据、跨章同键双值、近重复、高频片段。

本模块的全部判据都**题材无关**：观测（键邻近的数词、后缀族称谓、年式、集合成员、
清单计数、章头/标记/引号）由代码提供，**取值域**由项目层 `config.fact_keys` 声明。
因此本文件里的夹具一律使用抽象占位命名（张甲/李乙、玄铁令、三百二十株…），
不携带任何具体作品的人名、地名、年份、势力或年代物件——制度层的零内容纪律
由 `test_neutrality_guard.py` 单独守卫。

默认零误报是硬要求：`fact_keys` 留空、`quant_keys` 留空时，全部探针必须恒空输出。
"""

from __future__ import annotations

from novel_ledger_core.content.consistency import (
    chapter_format_issues,
    chapter_header_style_issues,
    count_conflicts,
    cross_chapter_key_conflicts,
    date_year_conflicts,
    entity_name_conflicts,
    entity_suffix_conflicts,
    fact_key_conflicts,
    near_duplicate_passages,
    number_conflicts,
    quote_style_issues,
    repeated_phrases,
    set_member_conflicts,
)

# —— 抽象夹具：与任何具体作品无关 ——

_LONG_A = (
    "他推开那扇掉了漆的木门走进院子，看见堂屋的桌上摊着一本旧册子，"
    "封皮已经磨得发了白，边角卷起来露出一截发黄的纸芯。"
    "他伸手把册子翻开，一笔一笔记下当日的开销，又抬头看了看窗外渐渐暗下来的天色。"
)
_LONG_B = _LONG_A.replace("当日的开销", "昨日的开销")
_UNRELATED = (
    "她蹲在灶前添了两根柴，锅里的水咕嘟咕嘟地响，屋里慢慢飘起米香。"
    "孩子趴在门槛上数着过往的骡车，数到第七辆就忘了数到几，重新从头数起。"
)


# —— 1. 实体称谓：后缀族取值域 ——

def test_entity_suffix_conflict_reports_only_declared_suffix_family():
    prose = "张甲一家四口住在巷口。隔年又写成李乙家四口人，还有赵宅的旧门牌。"
    hits = entity_suffix_conflicts(prose, key="k_household", canonical="张甲", suffixes=["家"])
    observed = [hit["observed"] for hit in hits]
    assert observed == ["李乙家"]
    # 声明过的规范称谓（含量词尾巴）不得报
    assert entity_suffix_conflicts("张甲家的门关着。", key="k", canonical="张甲", suffixes=["家"]) == []


def test_entity_suffix_conflict_is_silent_without_declaration():
    prose = "张甲一家四口。李乙家四口人。"
    assert entity_suffix_conflicts(prose, key="k", canonical="张甲", suffixes=[]) == []


# —— 2. 数值取值域 ——

def test_number_conflict_reports_other_value_near_key():
    prose = "他当时十一岁。后来的一处追述却写他十六岁。"
    hits = number_conflicts(prose, key="岁", canonical=11)
    assert [hit["observed"] for hit in hits] == [16]
    assert hits[0]["code"] == "fact_value_conflict"
    assert hits[0]["kind"] == "number"


def test_number_conflict_tolerates_same_value_rewritten():
    # 同值异写（十一 / 11）不是冲突：观测值解析后相等即可
    prose = "他当时十一岁，档案里也记着他 11 岁那年。"
    assert number_conflicts(prose, key="岁", canonical=11) == []


def test_number_conflict_tolerance_absorbs_declared_range():
    prose = "船票钱三百二十文。另记三百四十文。"
    assert [hit["observed"] for hit in number_conflicts(prose, key="船票钱", canonical=320, tolerance=20)] == []


# —— 3. 年式取值域 ——

def test_date_conflict_normalizes_chinese_and_arabic_years():
    prose = "那件事发生在八九年冬。他后来又说是一九九〇年。"
    hits = date_year_conflicts(prose, key="那件事", canonical=1989)
    assert [hit["observed"] for hit in hits] == [1990]


def test_date_conflict_same_year_two_spellings_is_not_a_conflict():
    prose = "八九年入冬。档案写成一九八九年。"
    assert date_year_conflicts(prose, key="入冬", canonical=1989) == []


def test_date_conflict_uses_project_declared_era_base():
    # 项目声明纪年基准后，省略式纪年与公历年可以互相比对
    prose = "火起在八九年冬。另一处写它是一九八八年的事。"
    hits = date_year_conflicts(prose, key="火起在", canonical=1989, era_map={"base": 1900})
    assert [hit["observed"] for hit in hits] == [1988]


# —— 4. 互斥集合成员 ——

def test_set_member_conflict_reports_the_other_member():
    prose = "脚印左脚重。另一份描样记为右脚重。"
    hits = set_member_conflicts(prose, key="脚印", canonical="左脚重", allow=["右脚重"])
    assert [hit["observed"] for hit in hits] == ["右脚重"]
    assert hits[0]["canonical"] == "左脚重"


def test_set_member_conflict_ignores_declared_member():
    prose = "脚印左脚重，和描样一致。"
    assert set_member_conflicts(prose, key="脚印", canonical="左脚重", allow=["右脚重"]) == []


# —— 5. 清单计数 ——

def test_count_conflict_reports_inconsistent_total():
    prose = "现场一共四条人命。后面的起诉书写成五条人命。"
    hits = count_conflicts(prose, key="人命", canonical=4)
    assert [hit["observed"] for hit in hits] == [5]
    assert hits[0]["kind"] == "count"


# —— 6. 实体名与别名 ——

def test_entity_name_conflict_reports_near_form_not_declared_alias():
    prose = "张甲的卷宗在此。卷宗里另写着张丙的名字。"
    hits = entity_name_conflicts(prose, key="卷宗", canonical="张甲", aliases=["老张"])
    assert [hit["observed"] for hit in hits] == ["张丙"]


def test_entity_name_conflict_accepts_declared_aliases():
    prose = "张甲的卷宗。老张翻过这一页。"
    assert entity_name_conflicts(prose, key="卷宗", canonical="张甲", aliases=["老张"]) == []


def test_entity_name_conflict_does_not_bite_into_verbs():
    # 贪婪匹配会切出「老张翻过」这类串：功能字判据把它挡在名字之外
    prose = "老张翻过卷宗，张甲的东西都在。"
    assert entity_name_conflicts(prose, key="卷宗", canonical="张甲", aliases=["老张"]) == []


# —— 7. 声明表驱动：形状与默认零输出 ——

def test_fact_key_conflicts_dispatch_by_declared_kind():
    prose = "他当时十一岁，后来写他十六岁。"
    spec = {"kind": "number", "observe": "岁", "canonical": 11}
    hits = fact_key_conflicts(prose, spec, key="k_age", chapter=7)
    assert [hit["observed"] for hit in hits] == [16]
    assert hits[0]["chapter"] == 7


def test_fact_key_conflicts_is_silent_for_shape_without_canonical():
    prose = "他当时十六岁。"
    assert fact_key_conflicts(prose, {"kind": "number", "observe": "岁"}, key="k") == []
    assert fact_key_conflicts(prose, {"kind": "number", "canonical": 11}, key="k") == []
    assert fact_key_conflicts(prose, {"kind": "unknown_kind", "observe": "岁", "canonical": 11}, key="k") == []


def test_empty_declaration_table_yields_zero_output():
    """`fact_keys` 留空 = 全部事实探针恒空：默认配置必须零误报。"""
    prose = "张甲一家四口。李乙家四口人。他十一岁，也写十六岁。"
    for kind, spec in (
        ("entity", {"kind": "entity", "suffixes": [], "canonical": "张甲"}),
        ("number", {"kind": "number", "observe": "", "canonical": 11}),
    ):
        assert fact_key_conflicts(prose, spec, key=kind) == []


# —— 8. 跨章同键双值 ——

def test_cross_chapter_key_conflict_lists_values_with_chapters():
    chapters = [(3, "押运的抽成是一成五。"), (91, "这一趟押运的抽成改成两成。")]
    hits = cross_chapter_key_conflicts(chapters, ["抽成"])
    assert len(hits) == 1
    assert hits[0]["code"] == "cross_chapter_key_conflict"
    assert len(hits[0]["values"]) == 2


def test_cross_chapter_key_conflict_is_opt_in():
    chapters = [(3, "押运的抽成是一成五。"), (91, "这一趟押运的抽成改成两成。")]
    assert cross_chapter_key_conflicts(chapters, []) == []


def test_cross_chapter_key_conflict_same_value_is_silent():
    chapters = [(3, "月租一百二十文。"), (91, "月租仍是一百二十文。")]
    assert cross_chapter_key_conflicts(chapters, ["月租"]) == []


# —— 9. 章节格式与体例 ——

def test_chapter_format_reports_duplicated_header_and_stage_markers():
    prose = "第1章 起局\n正文一段。\n第1章 起局\n第二场 夜审\n（本章完）\n"
    codes = {issue["code"] for issue in chapter_format_issues(prose, chapter=1)}
    assert {"duplicated_chapter_header", "scene_break_marker", "story_marker_residue"} <= codes


def test_chapter_format_reports_header_number_mismatch():
    issues = chapter_format_issues("第2章 归案\n正文。\n", chapter=3)
    assert [issue["code"] for issue in issues] == ["chapter_header_mismatch"]
    assert issues[0]["header_number"] == 2


def test_chapter_format_accepts_clean_prose():
    assert chapter_format_issues("第3章 归案\n\n他推门进来，屋里没人。\n", chapter=3) == []


def test_quote_style_mixed_within_one_chapter_is_reported():
    prose = "他说：「走吧。」隔了一会儿又说：\"行。\""
    assert [issue["code"] for issue in quote_style_issues(prose)] == ["quote_style_mixed"]


def test_quote_style_unpaired_is_reported():
    assert [issue["code"] for issue in quote_style_issues("他说：「走吧。」又漏了一个「")] == ["quote_unpaired"]


def test_quote_style_locked_by_project_config():
    prose = "他说：「走吧。」"
    assert quote_style_issues(prose, canonical_style="cn_corner") == []
    locked = quote_style_issues(prose, canonical_style="cn_double")
    assert [issue["code"] for issue in locked] == ["quote_style_mixed"]


def test_chapter_header_style_mixed_across_book():
    chapters = [(1, "第一章 起\n正文"), (2, "第2章 承\n正文")]
    assert [issue["code"] for issue in chapter_header_style_issues(chapters)] == ["chapter_header_style_mixed"]


def test_chapter_header_style_consistent_is_silent():
    chapters = [(1, "第1章 起\n正文"), (2, "第2章 承\n正文")]
    assert chapter_header_style_issues(chapters) == []


# —— 10. 跨章近重复段落 ——

def test_near_duplicate_passage_finds_the_same_scene_written_twice():
    hits = near_duplicate_passages([(12, _LONG_A), (40, _LONG_B)])
    assert hits and hits[0]["chapters"] == [12, 40]
    assert hits[0]["similarity"] >= 0.85


def test_near_duplicate_passage_is_silent_for_unrelated_prose():
    assert near_duplicate_passages([(12, _LONG_A), (40, _UNRELATED)]) == []


def test_near_duplicate_passage_ignores_same_chapter_repetition():
    # 章内重复不属本探针（章内复读由高频片段榜与审稿处理）
    assert near_duplicate_passages([(12, _LONG_A + _LONG_A)]) == []


def test_near_duplicate_passage_reports_each_span_once():
    hits = near_duplicate_passages([(1, _LONG_A), (2, _LONG_A)])
    assert len(hits) == 1


# —— 11. 全书高频片段 ——

def test_repeated_phrase_ranks_by_count_and_respects_threshold():
    chapters = [(number, "他把本子合上。" * 3) for number in range(1, 5)]
    hits = repeated_phrases(chapters, min_count=4)
    assert hits and all(hit["count"] >= 5 for hit in hits)
    assert any("本子" in hit["phrase"] for hit in hits)


def test_repeated_phrase_silent_below_threshold():
    assert repeated_phrases([(1, "他把本子合上。")], min_count=4) == []
