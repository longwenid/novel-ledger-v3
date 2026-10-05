import unittest
from pathlib import Path
import json
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from novel_ledger_core.content.gates import validate_write_output
from novel_ledger_core.content.pack import assemble_pack, slice_kb
from novel_ledger_core.control.pipeline import audit_hooks, close_hook, defer_hook, resync_baseline
from novel_ledger_core.ledger.ledger import load_snapshot, commit_event, verify_ledger
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from tests.host_tmp import default_mode_tempdir


class TestEnhancedFeatures(unittest.TestCase):
    def setUp(self):
        # 不用 tempfile.TemporaryDirectory()：它固定按 0o700 建目录，而在这类宿主沙箱里
        # 0o700 目录不可写入也不可枚举，setUp 第一行就会 Errno 13。根因见 tests/host_tmp.py。
        self.project_path = default_mode_tempdir("enhanced-")
        plan_path = self.project_path / "plan.json"
        plan_path.write_text(json.dumps({
            "protagonist": "主角",
            "chapters": [{
                "chapter": 1,
                "location": "市集",
                "present": ["主角"],
                "beats": [{"id": "b1", "required": True, "text": "主角进入市集", "must": "市集"}],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        init_project(
            self.project_path,
            plan_path=plan_path,
            protagonist="主角",
            word_min=50,
            word_max=1000,
        )
        self.store = BookStore(self.project_path)

    def tearDown(self):
        shutil.rmtree(self.project_path, ignore_errors=True)

    def test_glossary_gate_interception(self):
        pack = {
            "pack_hash": "test_hash",
            "glossary": {"旧写法": "规范写法"},
            "word_band": {"min": 10, "max": 1000},
            "beats": [{"id": "b1", "required": True, "must": "银币"}],
            "now_card": {"name": "主角"},
            "state_near": {"pinned": []},
        }
        output = {
            "pack_hash": "test_hash",
            "prose": "主角按旧写法记账，手里握着银币。",
            "l1_summary": "主角数银币。",
            "state_delta": {"named": ["主角"], "facts": []},
            "memory": {},
            "beats_hit": ["b1"],
        }
        issues = validate_write_output(output, pack, enforce_word_band=False)
        self.assertTrue(any(i.get("code") == "glossary_term_banned" and i.get("wrong_term") == "旧写法" for i in issues))

    def test_glossary_identity_mapping_not_banned(self):
        pack = {
            "pack_hash": "test_hash",
            "glossary": {"规范词": "规范词", "旧写法": "新规范"},
            "beats": [{"id": "b1", "text": "测试", "must": "银币"}],
            "allowed_delta_names": ["主角"],
            "state_near": {"pinned": []},
        }
        output = {
            "pack_hash": "test_hash",
            "prose": "主角手握银币，口诵规范词，心无杂念。",
            "l1_summary": "主角念规范词。",
            "state_delta": {"named": ["主角"], "facts": []},
            "memory": {},
            "beats_hit": ["b1"],
        }
        issues = validate_write_output(output, pack, enforce_word_band=False)
        self.assertFalse(any(i.get("wrong_term") == "规范词" for i in issues))

    def test_layered_kb_slicing_protagonist_isolation(self):
        cards = [
            {"id": "c1_plan", "title": "06_volume_一_plan", "body": "主角在第一卷的全部规划大纲", "tags": ["plan"]},
            {"id": "c2_char", "title": "掌柜人设卡", "body": "掌柜，客栈老板，与主角相依", "tags": ["character"]},
        ]
        res, _meta = slice_kb(
            cards,
            location="小巷",
            present=["主角"],
            tags=[],
            beats=[{"text": "在小巷漫步"}],
            cap=5,
            excerpt_chars=200,
            protagonist="主角",
        )
        self.assertFalse(any(c["id"] == "c1_plan" for c in res))

    def test_hooks_lifecycle_management(self):
        # 写入一条合法的包含 hook 的事件
        delta = {
            "named": ["主角"],
            "facts": [],
            "hooks": [{"id": "h_test_1", "due": 10, "status": "open", "text": "测试伏笔1"}],
        }
        commit_event(self.store, chapter=1, state_delta=delta)

        # 1. 审计
        audit_res = audit_hooks(self.store)
        self.assertEqual(audit_res["active_count"], 1)

        # 2. 延期
        defer_res = defer_hook(self.store, hook_id="h_test_1", new_due=50)
        self.assertEqual(defer_res["new_due"], 50)
        snap = load_snapshot(self.store)
        self.assertEqual(snap["hooks"][0]["due"], 50)

        # 3. 关闭
        close_res = close_hook(self.store, hook_id="h_test_1", reason="剧情自然消散")
        self.assertEqual(close_res["action"], "hook_closed")
        snap = load_snapshot(self.store)
        self.assertEqual(snap["hooks"][0]["status"], "closed")

    def test_hooks_governance_keeps_event_chain_green(self):
        """治理事件只追加到链尾，close/defer 不得把 verify 治红。"""
        hook_delta = {
            "named": ["主角"],
            "facts": [],
            "hooks": [{"id": "h_mid", "due": 2, "status": "open", "text": "中段伏笔"}],
        }
        commit_event(self.store, chapter=1, state_delta=hook_delta)
        for ch in (2, 3, 4):
            commit_event(self.store, chapter=ch, state_delta={"named": ["主角"], "facts": []})

        # 治理前链是绿的
        self.assertTrue(verify_ledger(self.store)["event_chain_ok"])

        defer_hook(self.store, hook_id="h_mid", new_due=30)
        verified = verify_ledger(self.store)
        # 本测试直接 commit_event、无正文文件，diffs 必有 missing_chapter_files；
        # 回归目标只是哈希链：治理后不得出现 event_hash_mismatch / event_chain_broken。
        self.assertTrue(verified["event_chain_ok"], verified.get("event_chain_issues"))
        self.assertNotIn(
            "event_chain", "".join(verified.get("diffs") or []),
        )
        self.assertEqual(load_snapshot(self.store)["hooks"][0]["due"], 30)

        close_hook(self.store, hook_id="h_mid", reason="已兑现")
        verified = verify_ledger(self.store)
        self.assertTrue(verified["event_chain_ok"], verified.get("event_chain_issues"))
        self.assertNotIn(
            "event_chain", "".join(verified.get("diffs") or []),
        )
        self.assertEqual(load_snapshot(self.store)["hooks"][0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
