"""Relevant context is selected by task, and selected material remains complete."""
from __future__ import annotations

import json

from novel_ledger_core.content.extract import extract_cards
from novel_ledger_core.content.hierarchical_memory import memory_for_pack, rebuild_hierarchical_memory
from novel_ledger_core.content.pack import assemble_pack, near_summaries, select_character_profiles, slice_kb
from novel_ledger_core.content.recall import memory_for_chapter, recall
from novel_ledger_core.content.views import make_draft_view, make_polish_view
from novel_ledger_core.infra.util import atomic_text
from novel_ledger_core.voice.anchor import select_anchor_slices
from novel_ledger_core.voice.voice_pack import voice_for_pack
from novel_ledger_core.voice.voice_manual import parse_skill_md
from tests.test_hierarchical_memory import _chapter, _store, _write_l1
from tests.test_sqlite_memory import history
from tests.test_autopilot import _store as _history_store


def test_long_relevant_kb_tail_is_found_and_kept_without_loading_unrelated_card():
    complete = "古城守门人的规则记录。" * 1000 + "尾部密钥必须先由旧城守门人核实。"
    cards = [{"id": "city-rule", "kind": "rule", "title": "本地规则", "body": complete,
              "tags": [f"标签{number}" for number in range(20)], "source": "city.md"},
             {"id": "remote-rule", "kind": "rule", "title": "远海规则", "body": "远海舰队不能靠岸。" * 1000}]
    selected, _ = slice_kb(cards, location="客栈", present=[], tags=[],
                           beats=[{"must": "尾部密钥", "text": "核实密钥"}], cap=1, excerpt_chars=1)
    assert [card["id"] for card in selected] == ["city-rule"]
    assert selected[0]["excerpt"] == complete
    assert selected[0]["tags"] == cards[0]["tags"]
    assert selected[0]["source"] == "city.md"


def test_explicit_card_refs_keep_every_requested_complete_card_beyond_default_page():
    cards = [{"id": f"rule-{number}", "kind": "rule", "title": "未有词面重合的规则",
              "body": "完整规则正文。" * 300 + f"尾部证据{number}"} for number in range(5)]
    selected, meta = slice_kb(cards, location="", present=[], tags=[], beats=[], cap=1,
                              excerpt_chars=1, kb_refs=[card["id"] for card in cards])
    assert {card["id"]: card["excerpt"] for card in selected} == {card["id"]: card["body"] for card in cards}
    assert meta["omitted_cards"] == 0


def test_draft_context_keeps_long_related_goal_recap_summary_and_tail_in_full(tmp_path):
    store = _store(tmp_path, [_chapter(number) for number in range(1, 4)])
    plan = store.load_plan()
    current = plan["chapters"][1]
    goal = "当前章需要达成的目标。" * 300 + "目标最后一项也必须完整"
    recap = "与本章直接相连的旧事。" * 300 + "前情末尾的必要因果"
    summary = "上一章已发生的关键事实。" * 300 + "近章摘要末尾的必要代价"
    current.update(goal=goal, recap=recap)
    store.save_plan(plan)
    cfg = store.load_config()
    cfg["pack_caps"].update(now_card_chars=1, history_chars=1, kb_excerpt_chars=1, story_focus_chars=1)
    store.save_config(cfg)
    _write_l1(store, 1, summary)
    rebuild_hierarchical_memory(store, 1)
    final_paragraph = "上章最后一个场景的完整动作。" * 400 + "上一章尾段最后一句必须承接"
    atomic_text(store.chapter_md_path(1), "更早的不相关开篇。\n一段。\n二段。\n三段。\n" + final_paragraph)
    pack = assemble_pack(store, 2)
    assert pack["now_card"]["goal"] == goal and goal in pack["now_card"]["rendered"]
    assert pack["recap"] == recap
    assert pack["near_summaries"][0]["l1_summary"] == summary
    assert pack["previous_chapter_tail"].endswith(final_paragraph)
    assert "更早的不相关开篇" not in pack["previous_chapter_tail"]
    brief = make_draft_view(pack)["writing_brief"]
    for selected in (goal, recap, summary, final_paragraph):
        assert selected in brief
    assert str(store.hierarchical_memory_path) in brief
    assert "memory_layers" not in make_polish_view(pack)


def test_hierarchy_keeps_long_current_phase_without_injecting_old_volume_history(tmp_path):
    store = _store(tmp_path, [_chapter(1, volume=1, phase="old"),
                              _chapter(2, volume=2, phase="current"), _chapter(3, volume=2, phase="current")])
    old = "远古卷中已结束且与当前任务无关的事件。" * 600 + "无关旧卷末尾标记"
    related = "当前阶段仍需追索的因果与代价。" * 600 + "当前阶段不可丢失的尾部标记"
    _write_l1(store, 1, old)
    _write_l1(store, 2, related)
    hierarchy = rebuild_hierarchical_memory(store, 2)
    assert old in hierarchy["volumes"][0]["summary"]
    layers = memory_for_pack(store, 3)
    rendered = json.dumps(layers, ensure_ascii=False)
    assert related in layers["phase_summary"]["summary"]
    assert related in layers["volume_summary"]["summary"]
    assert old not in rendered and "无关旧卷末尾标记" not in rendered
    assert layers["book_spine"]["source_path"] == str(store.hierarchical_memory_path)
    assert layers["book_spine"]["volume_index"][0]["id"] == "vol-0001"


def test_recall_keeps_complete_long_evidence_and_complete_matching_paragraph(tmp_path):
    store = _history_store(tmp_path, chapters=3)
    quote = "柜底旧约的完整正文证据。" * 700 + "柜底证据不可丢失的尾部"
    history(store, 1, quote=quote, text=quote)
    history(store, 2, who="丙", target="丁", quote="无关人物去远海航行", topic="无关")
    result = recall(store, before_chapter=3, people=["甲"], max_chars=1)
    assert any(row["text"] == quote and row["quote"] == quote for row in result["records"])
    assert all(row["chapter"] == 1 for row in result["records"])
    passages = recall(store, before_chapter=3, query="柜底证据不可丢失的尾部", max_chars=0)["records"]
    assert any(row["kind"] == "passage" and row["quote"] == quote + "。" for row in passages)
    cfg = store.load_config()
    cfg["pack_caps"]["history_chars"] = 0
    store.save_config(cfg)
    material = memory_for_chapter(store, 3, {}, ["甲"])
    assert any(row["quote"] == quote for row in material["records"])


def test_anchor_scene_selection_preserves_whole_long_paragraph_and_short_beats():
    relevant = "围城时两人通过对白试探彼此的底线。" * 800 + "围城对白场景末尾标记"
    unrelated = "灶膛生火煮饭时添柴。" * 800 + "无关烹饪场景末尾标记"
    selected = select_anchor_slices(unrelated + "\n他沉默。\n" + relevant, chapter=2,
                                    chars=1, slices=1, focus="围城对白")
    assert relevant in selected and unrelated not in selected
    explicit = select_anchor_slices(unrelated + "\n他沉默。\n" + relevant, chapter=2,
                                    chars=1, slices=1, paragraph_ids=[1, 2])
    assert "他沉默。" in explicit and relevant in explicit and unrelated not in explicit


def test_selected_voice_concepts_and_notes_are_complete_despite_legacy_cap():
    concepts = ["当前手册的完整概念。" * 300 + str(number) for number in range(5)]
    notes = ["当前书已有反馈。" * 300 + str(number) for number in range(3)]
    pack = voice_for_pack(concepts, session_notes=notes, cap=1)
    assert pack["concepts"] == concepts + notes


def test_compiled_relevant_source_sections_keep_long_body_tags_and_aliases(tmp_path):
    source = tmp_path / "canon"
    source.mkdir()
    body = "原始设定正文中不应被硬截断的完整条件。" * 300 + "原始正典末尾红线"
    tags = [f"相关标签{number}" for number in range(20)]
    aliases = [f"别名{number}" for number in range(20)]
    atomic_text(source / "rules.json", json.dumps({"cards": [{"title": "世界铁律", "kind": "rule", "hardness": "hard",
                                                               "body": body, "tags": tags, "aliases": aliases}]}, ensure_ascii=False))
    cards = extract_cards(source, excerpt_max=1)["cards"]
    assert len(cards) == 1 and cards[0]["body"] == body
    assert set(tags) <= set(cards[0]["tags"])
    assert cards[0]["aliases"] == aliases


def test_long_in_scene_character_profile_is_complete_without_loading_offstage_profile():
    relevant = "当场压力下的说话习惯和人物动机。" * 500 + "人物档案尾部的关键依据"
    offstage = "远海角色的旧日习惯。" * 500 + "未出场人物档案尾标记"
    selected = select_character_profiles({"主角": {"under_pressure": relevant},
                                          "远海角色": {"under_pressure": offstage}}, ["主角"])
    assert selected == {"主角": {"under_pressure": relevant}}


def test_long_selected_voice_rule_and_hard_rule_are_not_silently_skipped(tmp_path):
    concept = "完整的叙述概念及其必要条件。" * 200 + "长文风概念尾部标记"
    hard = "不可删去的作者硬规则及其完整理由。" * 200 + "长硬禁尾部标记"
    checklist = "交付前需要逐项核实的完整证据。" * 200 + "长自检尾部标记"
    path = tmp_path / "manual.md"
    atomic_text(path, f"# 文风\n> **完整公式**\n\n## 1. 叙述\n- {concept}\n\n### 硬禁\n- {hard}\n\n## 自检\n1. {checklist}\n")
    parsed = parse_skill_md(path)
    assert concept in parsed["concepts"] and hard in parsed["concepts"]
    assert parsed["checklist"] == [checklist]


def test_recent_summary_selection_reads_only_chosen_full_material(tmp_path, monkeypatch):
    from novel_ledger_core.content import pack as pack_module
    store = _store(tmp_path, [_chapter(number) for number in range(1, 12)])
    for number in range(1, 12):
        _write_l1(store, number, f"第{number}章完整摘要。" * 1000 + f"不可丢失的尾部{number}")
    original = pack_module.read_json
    loaded = []
    def read_selected(path):
        loaded.append(str(path))
        return original(path)
    monkeypatch.setattr(pack_module, "read_json", read_selected)
    selected, omitted = near_summaries(store, 10, cap=2)
    assert [row["chapter"] for row in selected] == [8, 9]
    assert loaded == [str(store.summary_path(8)), str(store.summary_path(9))]
    assert omitted == 7
    assert selected[0]["l1_summary"] == "第8章完整摘要。" * 1000 + "不可丢失的尾部8"
