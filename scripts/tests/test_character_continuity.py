import unittest

from novel_ledger_core.ledger.ledger import apply_event, get_character_continuity_records, select_relations
from novel_ledger_core.content.gates import validate_write_output, _continuity_issues
from novel_ledger_core.content.pack import _present_cards


class TestCharacterContinuity(unittest.TestCase):
    def test_relationship_slice_prioritizes_both_present_characters(self):
        relations = [
            {"who": "主角", "target": f"场外{i}", "kind": f"旧事{i}", "updated_chapter": i + 10}
            for i in range(10)
        ]
        relations.append({"who": "主角", "target": "掌柜", "kind": "本场旧约", "updated_chapter": 1})
        selected, omitted = select_relations(
            None, names=["主角", "掌柜"], cap=6, snapshot={"relations": relations}
        )
        self.assertEqual(selected[0]["kind"], "本场旧约")
        self.assertEqual(omitted, 5)

    def test_continuity_relations_share_bounded_pack_slice(self):
        relations = [
            {"who": "主角", "target": "掌柜", "kind": f"关系{i}", "status": "open", "updated_chapter": i}
            for i in range(1, 13)
        ]
        relations.append({"who": "主角", "target": "掌柜", "kind": "已结束", "status": "closed", "updated_chapter": 13})
        snapshot = {
            "entities": {"主角": {"facts": []}, "掌柜": {"facts": [], "first_seen_chapter": 1}},
            "relations": relations,
        }
        direct = get_character_continuity_records(snapshot, "主角", ["主角", "掌柜"], 100)
        direct_kinds = direct[1]["relations_with_protagonist"]
        self.assertEqual(direct_kinds, [f"关系{i}" for i in range(12, 6, -1)])

        selected = [{"who": "主角", "target": "掌柜", "kind": "关系2", "status": "open"}]
        packed = get_character_continuity_records(
            snapshot, "主角", ["主角", "掌柜"], 100, selected_relations=selected
        )
        self.assertEqual(packed[1]["relations_with_protagonist"], ["关系2"])

    def test_entity_chapter_tracking(self):
        snap = {
            "chapter": 0,
            "entities": {},
            "debts": [],
            "hooks": [],
            "relations": [],
            "occupancy": {},
        }
        
        # Event 1: Chapter 1 introduces 主角 and 掌柜
        ev1 = {
            "chapter": 1,
            "state_delta": {
                "named": ["主角"],
                "new_names": ["掌柜"],
                "facts": [{"who": "掌柜", "text": "集市商贩", "pin": False}],
                "relations": [{"who": "主角", "target": "掌柜", "kind": "旧识", "status": "open"}],
            }
        }
        snap = apply_event(snap, ev1)
        self.assertEqual(snap["entities"]["掌柜"]["first_seen_chapter"], 1)
        self.assertEqual(snap["entities"]["掌柜"]["last_seen_chapter"], 1)
        
        # Event 2: Chapter 20 interacts with 掌柜
        ev2 = {
            "chapter": 20,
            "state_delta": {
                "named": ["掌柜"],
                "facts": [{"who": "掌柜", "text": "被主角救过一次", "pin": True}],
            }
        }
        snap = apply_event(snap, ev2)
        self.assertEqual(snap["entities"]["掌柜"]["first_seen_chapter"], 1)
        self.assertEqual(snap["entities"]["掌柜"]["last_seen_chapter"], 20)

    def test_continuity_records_and_present_cards(self):
        snapshot = {
            "chapter": 20,
            "entities": {
                "主角": {"id": "主角", "first_seen_chapter": 1, "last_seen_chapter": 20, "facts": []},
                "掌柜": {"id": "掌柜", "first_seen_chapter": 20, "last_seen_chapter": 20, "facts": [{"text": "被救过一次", "pin": True}]},
                "新人": {"id": "新人", "first_seen_chapter": 21, "last_seen_chapter": 21, "facts": []},
            },
            "relations": [
                {"who": "主角", "target": "掌柜", "kind": "旧识", "status": "open"}
            ],
            "debts": [],
            "hooks": [],
            "occupancy": {},
        }
        
        records = get_character_continuity_records(snapshot, "主角", ["主角", "掌柜", "新人"], current_chapter=21)
        self.assertEqual(len(records), 3)
        
        # 掌柜 was seen in ch 20, so for ch 21 it's NOT first appearance
        keeper = next(r for r in records if r["name"] == "掌柜")
        self.assertFalse(keeper["is_first_appearance"])
        self.assertIn("重大连续性纪律", keeper["continuity_directive"])
        self.assertIn("旧识", keeper["relations_with_protagonist"])
        
        # 新人 is seen in ch 21 (current chapter), so it IS first appearance
        new_guy = next(r for r in records if r["name"] == "新人")
        self.assertTrue(new_guy["is_first_appearance"])

    def test_continuity_records_and_present_cards(self):
        """失忆闸门必须有方向：已知角色遇到真陌生人不得误报，旧识互不认得必须拦。

        历史缺陷：只要已知角色名与「你是何人」同句出现就报，于是
        ①「主角向刚出场的角色问身份」这种最常规的登场写法被判失忆、整章返工；
        ②「主角打量着眼前这个陌生的老者」同样误报。
        两者都是"已知角色遇到真陌生人"，方向反了。

        这张表也吸收了原先单独的 `test_gates_detect_character_amnesia`：
        两个触发句式（素未谋面 / 你是何人）、好人卡不误报、以及**归因到谁**的断言
        都在这里一次覆盖——分开写时，那条用例的正例只是同一批句式的重复。
        """
        pack_base = {
            "pack_hash": "h",
            "chapter": 10,
            "word_band": {"min": 0, "max": 10**9},
            "beats": [{"id": "b1", "required": True, "must": ""}],
            "now_card": {"name": "韩立"},
            "state_near": {"pinned": []},
            "character_continuity": [],
            "glossary": [],
        }
        output = {
            "l1_summary": "s",
            "state_delta": {},
            "memory": {},
            "pack_hash": "h",
            "beats_hit": ["b1"],
        }
        han = {"name": "韩立", "is_first_appearance": False, "first_seen_chapter": 1}
        li = {"name": "厉飞雨", "is_first_appearance": False, "first_seen_chapter": 2}

        cases = [
            # (说明, 正文, present_cards, 是否应拦, 必须被点名的已知角色)
            ("主角向新面孔问身份", "山道上走来一名灰袍老者。韩立按住袖中短剑，沉声问道：“你是何人？”", [han], False, ()),
            ("主角打量陌生老者", "韩立打量着眼前这个陌生的老者，手按剑柄。", [han], False, ()),
            ("主角与真新角色素未谋面", "韩立与来客素未谋面，拱手报了名号。", [han], False, ()),
            ("众人初识一个真新人", "韩立初次见到那名灰袍老者。", [han], False, ()),
            ("旧识之间问身份", "厉飞雨站在檐下。韩立抬眼看他，问道：“你是何人？”", [han, li], True, ("韩立", "厉飞雨")),
            ("旧识被写成陌生面孔", "韩立打量着眼前这个陌生的厉飞雨，眉头一皱。", [han, li], True, ("厉飞雨",)),
            ("旧识被写成素未谋面", "韩立与厉飞雨素未谋面，气氛骤然紧张。", [han, li], True, ("厉飞雨",)),
            ("旧识间初识描写", "韩立初识厉飞雨，两人拱手见礼。", [han, li], True, ("厉飞雨",)),
        ]
        for label, prose, present, should_flag, must_name in cases:
            pack = {**pack_base, "present_cards": present}
            issues = [
                i
                for i in validate_write_output(
                    output, pack, enforce_word_band=False, prose=prose
                )
                if i.get("code") == "continuity_character_amnesia"
            ]
            self.assertEqual(
                bool(issues), should_flag, f"{label}: issues={issues}"
            )
            if must_name:
                # 归因必须落在被写失忆的已知角色身上：漏掉他（或只报现在在场的主角）就是假阴性。
                flagged = {i.get("who") for i in issues}
                self.assertTrue(
                    flagged & set(must_name), f"{label}: 应点名 {must_name}，实得 {flagged}"
                )

    def test_amnesia_also_reads_character_continuity_fallback(self):
        """已知角色只出现在 character_continuity（present_cards 为空）时，闸门同样必须拦。

        两个来源是并列的提取路径（gates._known_characters），只测 present_cards 会漏掉
        这条回退分支——它的存在意义正是「角色卡没进包，但连续性记录进了」。
        """
        pack = {
            "pack_hash": "h",
            "chapter": 21,
            "word_band": {"min": 0, "max": 10**9},
            "beats": [{"id": "b1", "required": True, "must": ""}],
            "present_cards": [],
            "character_continuity": [
                {"name": "掌柜", "is_first_appearance": False, "first_seen_chapter": 20}
            ],
            "now_card": {"name": "主角"},
            "state_near": {"pinned": []},
            "glossary": [],
        }
        output = {
            "pack_hash": "h",
            "l1_summary": "s",
            "state_delta": {},
            "memory": {},
            "beats_hit": ["b1"],
        }
        issues = [
            i
            for i in validate_write_output(
                output,
                pack,
                enforce_word_band=False,
                prose="主角走进客栈，打量着眼前的掌柜。这掌柜正是旧识，两人素未谋面。",
            )
            if i.get("code") == "continuity_character_amnesia"
        ]
        self.assertEqual([i.get("who") for i in issues], ["掌柜"])

    def test_dead_character_card_carries_explicit_warning(self):
        """账本里已死亡的角色若仍出现在 present，必须把“已死”写进角色卡与连续性指令。"""
        from unittest.mock import MagicMock

        snapshot = {
            "chapter": 2,
            "entities": {
                "掌柜": {
                    "id": "掌柜",
                    "first_seen_chapter": 1,
                    "last_seen_chapter": 2,
                    "location": "市集",
                    "dead": True,
                    "facts": [],
                }
            },
            "debts": [],
            "hooks": [],
            "relations": [],
            "occupancy": {"市集": ["掌柜"]},
        }
        cards = _present_cards(
            store=MagicMock(),
            present=["主角", "掌柜"],
            protagonist="主角",
            snapshot=snapshot,
            chapter=3,
            ch_plan={"volume": 1},
        )
        card = next(c for c in cards if c["name"] == "掌柜")
        self.assertIn("死亡警告", card.get("continuity_directive") or "")
        self.assertIn("账本已标记死亡", card.get("rendered") or "")

        records = get_character_continuity_records(snapshot, "主角", ["主角", "掌柜"], 3)
        record = next(r for r in records if r["name"] == "掌柜")
        self.assertIn("死亡警告", record["continuity_directive"])


if __name__ == "__main__":
    unittest.main()
