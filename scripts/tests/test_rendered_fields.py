"""测试角色卡片和beats的rendered字段渲染。

验证：
- present_cards包含rendered字段（角色小传）
- beats包含rendered字段（场景描述）
- rendered字段将结构化数据转换成自然语言
"""
from unittest.mock import MagicMock

from novel_ledger_core.content.pack import _present_cards, _normalize_beats


def _mock_store():
    """创建最小mock store。"""
    return MagicMock()


def test_character_card_rendered_full():
    """完整角色卡片应渲染成自然语言小传。"""
    snapshot = {
        "entities": {
            "张三": {
                "name": "张三",
                "first_seen": 1,
                "last_seen": 2,
                "location": "茶馆",
                "facts": [
                    {"text": "开茶馆为生", "pin": False},
                    {"text": "性格爽朗", "pin": False},
                    {"text": "好客", "pin": False}
                ],
            }
        },
        "relations": [
            {"who": "张三", "target": "主角", "kind": "朋友", "status": "open"}
        ]
    }

    cards = _present_cards(
        store=_mock_store(),
        present=["张三"],
        protagonist="主角",
        snapshot=snapshot,
        chapter=2
    )

    assert len(cards) == 1
    card = cards[0]

    # 验证rendered字段存在
    assert "rendered" in card

    rendered = card["rendered"]

    # 验证rendered包含关键信息
    assert "角色【张三】" in rendered
    assert "茶馆" in rendered
    assert "朋友" in rendered
    assert "开茶馆为生" in rendered


def test_character_card_rendered_minimal():
    """最小角色卡片也应有rendered字段。"""
    snapshot = {
        "entities": {
            "路人甲": {
                "name": "路人甲",
                "first_seen": 1,
                "last_seen": 1,
                "facts": [],
            }
        },
        "relations": []
    }

    cards = _present_cards(
        store=_mock_store(),
        present=["路人甲"],
        protagonist="主角",
        snapshot=snapshot,
        chapter=1
    )

    card = cards[0]
    assert "rendered" in card
    assert "角色【路人甲】" in card["rendered"]


def test_character_card_rendered_without_personality():
    """无性格的角色rendered不包含说话特点。"""
    snapshot = {
        "entities": {
            "李四": {
                "name": "李四",
                "first_seen": 1,
                "last_seen": 1,
                "location": "街边",
                "facts": [{"text": "过路人", "pin": False}],
            }
        },
        "relations": []
    }

    cards = _present_cards(
        store=_mock_store(),
        present=["李四"],
        protagonist="主角",
        snapshot=snapshot,
        chapter=1
    )

    rendered = cards[0]["rendered"]
    assert "角色【李四】" in rendered
    assert "街边" in rendered
    assert "过路人" in rendered
    assert "说话特点" not in rendered


def test_beats_rendered_with_must():
    """带must词的beat应渲染成场景描述。"""
    raw_beats = [
        {
            "id": "b1",
            "required": True,
            "text": "主角在柜台当面拒收改期，掌柜不让他把凭据拿走",
            "must": "拒收"
        },
        {
            "id": "b2",
            "required": True,
            "text": "凭据还扣在掌柜手里，两人为此来回争执",
            "must": "凭据"
        }
    ]

    beats = _normalize_beats(raw_beats)

    assert len(beats) == 2

    # 第一场
    beat1 = beats[0]
    assert "rendered" in beat1
    assert "第1场" in beat1["rendered"]
    assert "主角在柜台当面拒收改期" in beat1["rendered"]
    assert "关键词须带出：拒收" in beat1["rendered"]

    # 第二场
    beat2 = beats[1]
    assert "rendered" in beat2
    assert "第2场" in beat2["rendered"]
    assert "凭据还扣在掌柜手里" in beat2["rendered"]
    assert "关键词须带出：凭据" in beat2["rendered"]


def test_beats_rendered_without_must():
    """无must词的beat渲染时不显示关键词提示。"""
    raw_beats = [
        {
            "id": "b1",
            "required": True,
            "text": "两人在茶馆闲聊",
            "must": ""
        }
    ]

    beats = _normalize_beats(raw_beats)

    beat = beats[0]
    assert "rendered" in beat
    assert "第1场：两人在茶馆闲聊" == beat["rendered"]
    assert "关键词" not in beat["rendered"]


def test_beats_rendered_from_string_list():
    """从字符串列表创建的beat也应有rendered字段。"""
    raw_beats = ["主角来到茶馆", "与掌柜对话", "离开茶馆"]

    beats = _normalize_beats(raw_beats)

    assert len(beats) == 3
    assert beats[0]["rendered"] == "第1场：主角来到茶馆"
    assert beats[1]["rendered"] == "第2场：与掌柜对话"
    assert beats[2]["rendered"] == "第3场：离开茶馆"


def test_beats_rendered_mixed():
    """混合格式的beats列表。"""
    raw_beats = [
        "主角来到茶馆",
        {
            "id": "b2",
            "text": "与掌柜争执",
            "must": "凭据"
        },
        {
            "id": "b3",
            "text": "散场离开"
        }
    ]

    beats = _normalize_beats(raw_beats)

    assert len(beats) == 3
    assert beats[0]["rendered"] == "第1场：主角来到茶馆"
    assert "第2场：与掌柜争执" in beats[1]["rendered"]
    assert "关键词须带出：凭据" in beats[1]["rendered"]
    assert beats[2]["rendered"] == "第3场：散场离开"


def test_character_card_rendered_with_relations():
    """有多个关系的角色rendered应列出所有关系。"""
    snapshot = {
        "entities": {
            "王五": {
                "name": "王五",
                "first_seen": 1,
                "last_seen": 2,
                "location": "酒楼",
                "facts": [{"text": "酒楼老板", "pin": False}],
            }
        },
        "relations": [
            {"who": "王五", "target": "主角", "kind": "朋友", "status": "open"},
            {"who": "王五", "target": "主角", "kind": "债主", "status": "open"},
        ]
    }

    cards = _present_cards(
        store=_mock_store(),
        present=["王五"],
        protagonist="主角",
        snapshot=snapshot,
        chapter=2
    )

    rendered = cards[0]["rendered"]
    assert "角色【王五】" in rendered
    assert "与主角的关系：" in rendered
    assert "朋友" in rendered
    assert "债主" in rendered


def test_empty_beats_list():
    """空beats列表应返回空列表。"""
    beats = _normalize_beats([])
    assert beats == []

    beats = _normalize_beats(None)
    assert beats == []


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
