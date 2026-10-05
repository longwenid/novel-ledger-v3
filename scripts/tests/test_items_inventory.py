from __future__ import annotations

import unittest

from novel_ledger_core.ledger.ledger import (
    EMPTY_SNAPSHOT,
    apply_event,
    select_items,
)


class TestItemsInventory(unittest.TestCase):
    def test_recent_item_enters_pack_slice_after_old_inventory_fills_cap(self):
        snap = dict(EMPTY_SNAPSHOT)
        for chapter in range(1, 9):
            snap = apply_event(
                snap,
                {
                    "chapter": chapter,
                    "state_delta": {
                        "items": [
                            {
                                "id": f"old_{chapter}",
                                "name": f"旧物{chapter}",
                                "holder": "主角",
                                "status": "held",
                            }
                        ]
                    },
                },
            )
        snap = apply_event(
            snap,
            {
                "chapter": 1000,
                "state_delta": {
                    "items": [
                        {
                            "id": "new_key",
                            "name": "新得密钥",
                            "holder": "主角",
                            "status": "held",
                        }
                    ]
                },
            },
        )

        items, omitted = select_items(None, names=["主角"], cap=8, snapshot=snap)

        self.assertEqual(items[0]["id"], "new_key")
        self.assertEqual(len(items), 8)
        self.assertNotIn("old_1", [item["id"] for item in items])
        self.assertEqual(omitted, 1)

    def test_item_lifecycle_in_snapshot(self):
        snap = dict(EMPTY_SNAPSHOT)
        self.assertIn("items", snap)
        self.assertEqual(snap["items"], [])

        # 1. 获得物品
        ev1 = {
            "chapter": 1,
            "state_delta": {
                "named": ["主角"],
                "items": [
                    {
                        "id": "item_tie_jian",
                        "name": "青铜残剑",
                        "holder": "主角",
                        "quantity": 1,
                        "kind": "weapon",
                        "status": "held",
                        "quote": "从柜台底下摸出一柄青铜残剑",
                    }
                ],
            },
        }
        snap = apply_event(snap, ev1)
        self.assertEqual(len(snap["items"]), 1)
        self.assertEqual(snap["items"][0]["name"], "青铜残剑")
        self.assertEqual(snap["items"][0]["holder"], "主角")
        self.assertEqual(snap["items"][0]["status"], "held")

        # 2. 物品转移给他人
        ev2 = {
            "chapter": 2,
            "state_delta": {
                "named": ["主角", "掌柜"],
                "items": [
                    {
                        "id": "item_tie_jian",
                        "holder": "掌柜",
                        "status": "transferred",
                        "quote": "将青铜残剑递到了掌柜手中",
                    }
                ],
            },
        }
        snap = apply_event(snap, ev2)
        self.assertEqual(len(snap["items"]), 1)
        self.assertEqual(snap["items"][0]["holder"], "掌柜")
        self.assertEqual(snap["items"][0]["status"], "held")
        self.assertEqual(snap["items"][0]["updated_chapter"], 2)

        # 3. 下一章的在场人物资产切片：新持有人能用，旧持有人不能用。
        recipient_items, _ = select_items(None, names=["掌柜"], snapshot=snap)
        donor_items, _ = select_items(None, names=["主角"], snapshot=snap)
        self.assertEqual([item["id"] for item in recipient_items], ["item_tie_jian"])
        self.assertEqual(donor_items, [])
        self.assertEqual(ev2["state_delta"]["items"][0]["status"], "transferred")


if __name__ == "__main__":
    unittest.main()
