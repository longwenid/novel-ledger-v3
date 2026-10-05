"""commit 崩溃收敛测试：failpoint 参数化（吸收 novel-forge test_submission_recovery 模式）。

一次 commit 的写入序列是 events → snapshot → 章 md → meta → summary → 滚层 → 文风记忆 →
HEAD。这里在序列的每个边界注入一次崩溃，然后只重跑 `chapter next`，断言**每个崩溃点都
收敛**：进入 ack、账本 verify 绿、facts 不翻倍、哈希链完整——不需要人工 retry-authorize。

前滚的安全边界由 `_resumable_commit` 把守：只有"同一份已验收提交"才续跑；delta 分歧的
残留仍然保守 blocked（本文件最后一个用例）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.conftest import patch_pipeline_name

from novel_ledger_core.control import pipeline as pipeline
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import chapter_next, retry_authorize, submit_output
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json
from novel_ledger_core.ledger.ledger import read_events, verify_ledger
from tests.decoupled_helpers import advance_to_assembly, decoupled_submit

PROSE = (
    "主角站在市集当中，掌柜把账本往柜台上一压。"
    "主角当场拒收改期，凭据还压在柜台那头。"
    "两人隔着柜台来回说了几句，谁也没有让步，改天还得再来取。"
)
FACT = "主角拒收改期，凭据仍被扣。"


def _make_store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷。",
        "chapters": [
            {
                "chapter": 1,
                "volume": 1,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "beats": [
                    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": "凭据仍被扣", "must": "凭据"},
                ],
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False  # 测试正文远短于字数带，与 test_ops_commands 同口径
    store.save_config(cfg)
    return store


def _drive_to_submitted(store: BookStore) -> None:
    """走到 submit accepted、HEAD 停在 submitted（下一步 chapter next 才 commit）。"""
    advance_to_assembly(store, prose=PROSE, chapter=1)
    pack = read_json(store.assemble_pack_path(1))
    result = submit_output(
        store,
        {
            "prose": PROSE,
            "l1_summary": FACT,
            "state_delta": {
                "moves": [],
                "facts": [{"who": "主角", "text": FACT}],
                "debts": [],
                "hooks": [],
                "relations": [],
                "named": ["主角", "掌柜"],
                "new_names": [],
                "deaths": [],
            },
            "memory": {"voice_concepts": []},
            "pack_hash": pack["pack_hash"],
            "beats_hit": ["b1", "b2"],
        },
    )
    assert result["verdict"] == "accepted", result


class _Crash(RuntimeError):
    pass


def _install_failpoint(monkeypatch, name: str) -> None:
    """在 commit 写入序列的指定边界注入一次性（或全量）崩溃。"""
    if name == "after_events":
        real = pipeline.atomic_text

        def flaky(path, data, *args, **kwargs):
            raise _Crash("after_events")

        patch_pipeline_name(monkeypatch, "atomic_text", flaky)
    elif name in ("after_md", "after_meta"):
        real = pipeline.atomic_json
        target = 1 if name == "after_md" else 2
        counter = {"n": 0}

        def flaky(path, payload, *args, **kwargs):
            counter["n"] += 1
            if counter["n"] == target:
                raise _Crash(name)
            return real(path, payload, *args, **kwargs)

        patch_pipeline_name(monkeypatch, "atomic_json", flaky)
    elif name == "after_summary":
        def flaky(*args, **kwargs):
            raise _Crash("after_summary")

        patch_pipeline_name(monkeypatch, "update_hierarchical_memory", flaky)
    elif name == "after_hierarchy":
        def flaky(*args, **kwargs):
            raise _Crash("after_hierarchy")

        patch_pipeline_name(monkeypatch, "_merge_voice_memory", flaky)
    elif name == "before_head":
        def flaky(self, payload, *args, **kwargs):
            raise _Crash("before_head")

        monkeypatch.setattr(BookStore, "write_head", flaky)
    else:
        raise AssertionError(f"未知 failpoint: {name}")


def _facts_of(store: BookStore) -> list[str]:
    snap = read_json(store.snapshot_path)
    return [f["text"] for f in (snap["entities"].get("主角") or {}).get("facts") or []]


def _assert_converged(store: BookStore) -> None:
    report = verify_ledger(store)
    assert report["consistent"], report["diffs"]
    assert report["event_chain_ok"], report["event_chain_issues"]
    assert _facts_of(store) == [FACT], "崩溃重跑后 facts 必须恰好一条（不翻倍、不丢失）"
    assert len(read_events(store)) == 1
    head = store.read_head()
    assert head["last_committed_ch"] == 1
    meta = read_json(store.chapter_meta_path(1))
    assert head["prose_hash"] == meta["prose_hash"]
    assert store.chapter_md_path(1).exists()


@pytest.mark.parametrize(
    "failpoint",
    ["after_events", "after_md", "after_meta", "after_summary", "after_hierarchy", "before_head"],
)
def test_commit_converges_after_each_crash_point(tmp_path: Path, monkeypatch, failpoint: str):
    store = _make_store(tmp_path)
    _drive_to_submitted(store)

    _install_failpoint(monkeypatch, failpoint)
    with pytest.raises(_Crash):
        chapter_next(store)
    monkeypatch.undo()

    # 唯一的恢复动作：重跑 chapter next。前滚语义下不阻塞、不回滚、不需要人。
    resumed = chapter_next(store)
    assert resumed["ok"] is True, resumed
    assert resumed["action"] == "ack", resumed
    _assert_converged(store)


def test_divergent_delta_still_blocks_and_retry_authorize_still_works(tmp_path: Path, monkeypatch):
    """前滚边界是「同一份提交」：delta 分歧的崩溃残留必须保守 blocked。"""
    store = _make_store(tmp_path)
    _drive_to_submitted(store)

    _install_failpoint(monkeypatch, "after_events")
    with pytest.raises(_Crash):
        chapter_next(store)
    monkeypatch.undo()

    # 已入账事件被改成与落盘 output.json 不同的 delta（模拟两稿分歧 / 疑似污染）
    events = read_events(store)
    events[0]["state_delta"]["facts"][0]["text"] = FACT + "（分歧稿）"
    body = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events)
    store.events_path.write_text(body, encoding="utf-8")

    blocked = chapter_next(store)
    assert blocked["action"] == "blocked"
    assert blocked["blocked"]["reason"] == "ledger_replay_conflict"

    # 人工恢复通道保持可用：retry-authorize 撤销本章事件，重写后正常入账
    auth = retry_authorize(store, actor="human", reason="divergent crash residue")
    assert auth["verdict"] == "authorized"
    assert auth["ledger_rollback"]["removed_events"] == 1
    rewrite = decoupled_submit(
        store,
        {
            "prose": PROSE,
            "l1_summary": FACT,
            "state_delta": {
                "moves": [],
                "facts": [{"who": "主角", "text": FACT}],
                "debts": [],
                "hooks": [],
                "relations": [],
                "named": ["主角", "掌柜"],
                "new_names": [],
                "deaths": [],
            },
            "memory": {"voice_concepts": []},
            "pack_hash": read_json(store.current_pack_path)["pack_hash"],
            "beats_hit": ["b1", "b2"],
        },
    )
    assert rewrite["verdict"] == "accepted", rewrite
    assert chapter_next(store)["action"] == "ack"
    _assert_converged(store)
