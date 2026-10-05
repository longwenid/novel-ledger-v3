from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.content.views import make_assemble_view, render_writing_brief
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json
from novel_ledger_core.ledger.ledger import facts_for_pack


def _pack_with_beats(tmp_path: Path, beat_text: str, must: str) -> dict:
    plan = {
        "title": "事实相关性回归",
        "protagonist": "主角",
        "volume_spine": "主角追查旧案。",
        "chapters": [{
            "chapter": 1,
            "location": "客栈",
            "present": ["主角", "掌柜"],
            "beats": [{"id": "b1", "required": True, "text": beat_text, "must": must}],
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    snap = read_json(store.snapshot_path)
    snap["entities"] = {
        "主角": {
            "id": "主角", "location": "客栈", "first_seen_chapter": 1, "last_seen_chapter": 900,
            "facts": [
                {"text": "左腕旧伤使主角不能握剑", "pin": True},
                {"text": "主角曾在渡口见过船夫", "pin": True},
                {"text": "主角保管一张旧账单", "pin": True},
                {"text": "主角与药铺有旧约", "pin": True},
            ],
        },
        "掌柜": {
            "id": "掌柜", "location": "客栈", "first_seen_chapter": 2, "last_seen_chapter": 900,
            "facts": [
                {"text": "掌柜认得旧案的血印", "pin": True},
                {"text": "掌柜喜欢收集瓷碗", "pin": True},
                {"text": "掌柜欠药铺银两", "pin": True},
                {"text": "掌柜曾经营渡口茶棚", "pin": True},
            ],
        },
    }
    atomic_json(store.snapshot_path, snap)
    return assemble_pack(store, 1)


def test_old_pinned_facts_relevant_to_chapter_beats_are_consumed_across_pack(tmp_path: Path):
    pack = _pack_with_beats(
        tmp_path,
        "主角左腕旧伤复发，掌柜拿出旧案血印作证。",
        "旧案血印",
    )
    hero_fact = "左腕旧伤使主角不能握剑"
    keeper_fact = "掌柜认得旧案的血印"
    assert hero_fact in pack["now_card"]["facts"]
    assert hero_fact in next(x for x in pack["state_near"]["pinned"] if x["id"] == "主角")["facts"]
    assert hero_fact in next(x for x in pack["character_continuity"] if x["name"] == "主角")["facts"]
    assert keeper_fact in pack["present_cards"][0]["facts"]
    assert keeper_fact in next(x for x in pack["character_continuity"] if x["name"] == "掌柜")["facts"]
    assert all(len(x["facts"]) <= 3 for x in pack["state_near"]["pinned"])
    assert len(pack["now_card"]["facts"]) <= 3
    assert len(pack["present_cards"][0]["facts"]) <= 3

    # 事实选择保留在 canonical pack；阶段输入只投喂同一条事实一次。
    brief = render_writing_brief(pack)
    assemble = make_assemble_view(pack)
    assert brief.count(hero_fact) == 1
    assert brief.count(keeper_fact) == 1
    assert json.dumps(assemble, ensure_ascii=False).count(hero_fact) == 1
    assert json.dumps(assemble, ensure_ascii=False).count(keeper_fact) == 1


def test_no_fact_match_keeps_recent_pinned_selection(tmp_path: Path):
    pack = _pack_with_beats(tmp_path, "主角在客栈等天亮，掌柜合上店门。", "天亮")
    assert pack["now_card"]["facts"] == [
        "主角曾在渡口见过船夫", "主角保管一张旧账单", "主角与药铺有旧约",
    ]
    assert pack["present_cards"][0]["facts"] == [
        "掌柜喜欢收集瓷碗", "掌柜欠药铺银两", "掌柜曾经营渡口茶棚",
    ]


def test_specific_old_fact_beats_newer_facts_with_only_topic_overlap():
    facts = [
        {"text": "掌柜认得旧案的血印", "pin": True},
        {"text": "掌柜保管旧案卷宗", "pin": True},
        {"text": "掌柜认识旧案证人", "pin": True},
        {"text": "掌柜抄过旧案口供", "pin": True},
    ]
    selected = facts_for_pack(facts, cap=3, focus="旧案血印", who="掌柜")
    assert "掌柜认得旧案的血印" in selected
    assert len(selected) == 3


def test_writing_brief_carries_per_beat_word_allocation():
    """拍级配速：字数带已知时把目标摊到每场，写短当场可见；
    无字数带的 pack 不渲染配速标记（不造口径）。"""
    pack = {
        "chapter": 3,
        "word_band": {"min": 2500, "max": 3200},
        "beats": [
            {"id": "b1", "required": True, "text": "开场对峙", "must": "对峙"},
            {"id": "b2", "required": True, "text": "中段转折"},
            {"id": "b3", "required": False, "text": "收尾过场"},
        ],
    }
    brief = render_writing_brief(pack)
    # aim = min(2500+700, 3200) = 3200 → 等分 [1067, 1067, 1066]，余数给前几拍。
    assert "（本场目标约1067汉字）" in brief
    assert "（本场目标约1066汉字）" in brief
    assert brief.count("本场目标约") == 3
    assert "第1场必须完整演出：开场对峙（本场目标约1067汉字）；正文中必须自然出现“对峙”" in brief

    bare = render_writing_brief({"chapter": 1, "beats": pack["beats"]})
    assert "本场目标约" not in bare
