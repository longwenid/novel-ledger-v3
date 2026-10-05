from __future__ import annotations

import unittest

from novel_ledger_core.content.gates import (
    _unintroduced_familiarity_issues,
    validate_prose_anchors,
)
from novel_ledger_core.content.style_check import (
    check_tail_moralizing,
    _hard_fails,
)
from novel_ledger_core.content.numeric_audit import (
    algebraic_flow_mismatches,
)


class TestNovelEssentialDetails(unittest.TestCase):
    def test_unintroduced_familiarity_interception(self):
        pack = {
            "chapter": 10,
            "now_card": {"name": "韩立"},
            "present_cards": [
                {
                    "name": "李飞羽",
                    "is_first_appearance": True,
                    "first_seen_chapter": 10,
                }
            ],
        }

        # 1. 违规场景：未通名自报家门，对白直接直呼大名
        bad_prose = "山道转角走出一个黑袍青年。韩立抬手一抱拳，道：“李飞羽，你在此作甚？”"
        issues = _unintroduced_familiarity_issues(bad_prose, pack)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "continuity_unintroduced_familiarity")
        self.assertEqual(issues[0]["who"], "李飞羽")
        # 拒绝载荷自带豁免清单（镜像检测器 _INTRO_PATTERNS）——返工宿主不必读源码。
        from novel_ledger_core.content.gates import _INTRO_PATTERNS
        for marker in ("自称", "引见", "名帖"):
            self.assertIn(marker, issues[0]["hint"])
        self.assertIn("、".join(_INTRO_PATTERNS[:3]), issues[0]["hint"])

        # 2. 合规场景：有通名/自报家门
        good_prose = "山道转角走出一个黑袍青年，自称李飞羽。韩立抬手一抱拳，道：“李兄，你在此作甚？”"
        self.assertEqual(_unintroduced_familiarity_issues(good_prose, pack), [])

        # 3. 合规场景：引见介绍
        good_prose2 = "掌柜上前引见，原来是李飞羽到了。韩立拱手道：“久仰大名。”"
        self.assertEqual(_unintroduced_familiarity_issues(good_prose2, pack), [])

    def test_tail_moralizing_interception(self):
        # 1. 违规场景：末段出现 AI 升华总结
        bad_prose = (
            "韩立收起短剑，转身步入雨幕之中。\n\n"
            "他深知，未来的路还很长，而这一切，才刚刚开始。"
        )
        res = check_tail_moralizing(bad_prose)
        self.assertFalse(res["ok"])
        self.assertTrue(any("章尾AI升华与套话总结" in f["metric"] for f in res["fails"]))

        # 2. 违规场景：属于他的传奇
        bad_prose2 = (
            "城门在身后缓缓关上。\n\n"
            "属于他的传奇注定不会平静，命运的齿轮已然悄然转动。"
        )
        res2 = check_tail_moralizing(bad_prose2)
        self.assertFalse(res2["ok"])

        # 3. 合规场景：干净的动作定格与悬念留白
        good_prose = (
            "韩立吹灭案头油灯，侧身贴在窗棂后。\n\n"
            "院墙外的雨水顺着瓦当滴答作响，两道压抑的脚步声在门前停住了。"
        )
        self.assertTrue(check_tail_moralizing(good_prose)["ok"])

    def test_algebraic_flow_mismatch(self):
        # 1. 违规算术穿帮：原有 300 两，花去 100 两，还剩 150 两 (300 - 100 != 150)
        bad_prose = "他怀揣三百两银子出了客栈，买药材花去一百两，口袋里还剩一百五十两银子。"
        hits = algebraic_flow_mismatches(bad_prose)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["code"], "algebraic_flow_mismatch")
        self.assertEqual(hits[0]["initial"], 300)
        self.assertEqual(hits[0]["spent"], 100)
        self.assertEqual(hits[0]["computed_remain"], 200)
        self.assertEqual(hits[0]["stated_remain"], 150)

        # 2. 正确算术流水：原有 500 块灵石，耗费 200 块，剩下 300 块
        good_prose = "包里存有五百块灵石，购阵盘耗费二百块，如今手里还剩三百块，勉强够支撑开销。"
        self.assertEqual(algebraic_flow_mismatches(good_prose), [])


if __name__ == "__main__":
    unittest.main()
