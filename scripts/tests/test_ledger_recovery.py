"""账本事务性：同章重写的回滚、commit 幂等、verify 交叉校验、repair、写锁。

这些是唯一会**不可逆损坏数据**的一类缺陷：账本里留下废稿的事实/死亡，而废稿正文已删。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.decoupled_helpers import decoupled_submit, make_chapter, make_plan, write_plan
from novel_ledger_core.control.cli import main
from novel_ledger_core.ledger.ledger import (
    commit_event,
    max_event_chapter,
    read_events,
    repair_ledger,
    rollback_ledger_to,
    verify_ledger,
)
from novel_ledger_core.control.pipeline import ack_read, chapter_next, retry_authorize, submit_output
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json

QUOTES_DRAFT = ["主角在市集拒收", "市集拒收了改期", "拒收了改期的凭据"]


def _plan() -> dict:
    return make_plan(
        [
            make_chapter(1, tags=("码头", "凭据")),
            make_chapter(
                2,
                location="货栈",
                present=("主角",),
                tags=("货栈",),
                beats=[{"id": "b2", "required": True, "text": "主角夜里翻账", "must": "翻账"}],
            ),
        ]
    )


def _make_project(tmp_path: Path, name: str = "bookproj") -> Path:
    plan_path = write_plan(tmp_path, _plan())
    proj = tmp_path / name
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    return proj


def _output(store: BookStore, *, prose: str, fact: str, deaths: list[str] | None = None) -> dict:
    pack = read_json(store.current_pack_path)
    return {
        "prose": prose,
        "l1_summary": fact,
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": fact}],
            "debts": [{"id": "d-draft", "who": "主角", "text": fact, "status": "open"}],
            "hooks": [{"id": "h-draft", "text": fact, "due": 9, "status": "open"}],
            "relations": [{"who": "主角", "target": "掌柜", "kind": "旧交", "status": "open"}],
            "named": ["主角", "掌柜"],
            "new_names": [],
            "deaths": list(deaths or []),
        },
        "memory": {"voice_concepts": []},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
    }


def _draft_output(store: BookStore) -> dict:
    return _output(
        store,
        prose="主角在市集拒收了改期的凭据，掌柜当场断了气。",
        fact="废稿事实：掌柜死在市集",
        deaths=["掌柜"],
    )


def _rewrite_output(store: BookStore) -> dict:
    return _output(
        store,
        prose="主角在市集拒收了改期的凭据，掌柜只是拍了拍他的肩。",
        fact="重写事实：掌柜还活着",
    )


def _commit_draft(store: BookStore, build_output) -> None:
    """begin → submit → commit。build_output 必须在 pack 装配之后才读 pack_hash。"""
    chapter_next(store)
    r = decoupled_submit(store, build_output(store))
    assert r["verdict"] == "accepted", r
    committed = chapter_next(store)
    assert committed["action"] == "ack", committed


def _facts_of(store: BookStore, who: str) -> list[str]:
    snap = read_json(store.snapshot_path)
    return [f["text"] for f in (snap["entities"].get(who) or {}).get("facts") or []]


def test_reopen_rolls_back_ledger_events_and_deaths(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    _commit_draft(store, _draft_output)

    assert len(read_events(store)) == 1
    assert _facts_of(store, "主角") == ["废稿事实：掌柜死在市集"]
    assert read_json(store.snapshot_path)["entities"]["掌柜"]["dead"] is True

    reopened = ack_read(store, quotes=QUOTES_DRAFT, verdict="p0")
    assert reopened["verdict"] == "reopen"

    # 事件行数回退到 0；快照不再含废稿的事实/债务/钩子/关系
    assert read_events(store) == []
    snap = read_json(store.snapshot_path)
    assert _facts_of(store, "主角") == []
    assert snap["debts"] == []
    assert snap["hooks"] == []
    assert snap["relations"] == []
    # deaths 是单向不可逆的：apply_event 只设 dead=True，只能靠回滚撤销
    assert "掌柜" not in snap["entities"] or snap["entities"]["掌柜"].get("dead") is not True
    assert not store.chapter_md_path(1).exists()


def test_rewrite_after_reopen_has_no_ghost_facts(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    _commit_draft(store, _draft_output)
    assert ack_read(store, quotes=QUOTES_DRAFT, verdict="p0")["verdict"] == "reopen"

    # 重写稿提交入账：同一实体下不得出现两稿并存的事实
    r = decoupled_submit(store, _rewrite_output(store))
    assert r["verdict"] == "accepted"
    assert chapter_next(store)["action"] == "ack"
    assert _facts_of(store, "主角") == ["重写事实：掌柜还活着"]
    assert len(read_events(store)) == 1
    snap = read_json(store.snapshot_path)
    assert [d["text"] for d in snap["debts"]] == ["重写事实：掌柜还活着"]
    assert snap["entities"]["掌柜"].get("dead") is not True

    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    acked = ack_read(
        store,
        quotes=["主角在市集拒收", "掌柜只是拍了拍他的肩", "拒收了改期的凭据"],
        verdict="pass",
    )
    assert acked["verdict"] == "acked"
    assert "掌柜当场断了气" not in prose
    assert verify_ledger(store)["consistent"] is True


def test_recommit_after_crash_does_not_double_facts(tmp_path: Path):
    """模拟 commit 中途崩溃：HEAD 仍停在 submitted，重跑 next 不得让 facts 翻倍。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    _commit_draft(store, _rewrite_output)
    facts_before = _facts_of(store, "主角")
    assert len(facts_before) == 1
    events_before = len(read_events(store))

    head = store.read_head()
    head["phase"] = "submitted"
    head["last_committed_ch"] = 0
    store.write_head(head)

    again = chapter_next(store)
    assert again["ok"] is True
    # commit 崩溃恢复为前滚语义：
    # HEAD 停在 submitted 但账本里的本章事件与落盘 output.json 逐字节一致时，
    # 幂等尾部直接续跑进 ack——不再 blocked、不再要求人工整章回滚重写。
    # delta 分歧的残留仍走保守 blocked（见 test_commit_failpoints.py）。
    assert again["action"] == "ack"
    assert _facts_of(store, "主角") == facts_before
    assert len(read_events(store)) == events_before
    assert store.read_head()["last_committed_ch"] == 1
    assert verify_ledger(store)["consistent"] is True


def test_verify_detects_duplicate_chapter_events(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    # 只含 moves 的 delta 是幂等的：重放与快照仍逐字段相等，
    # 所以"同章两条事件"只能靠交叉校验发现，字段比对是盲的。
    commit_event(store, 1, {"moves": [{"who": "主角", "to": "市集"}], "named": ["主角"]})
    _ch = store.chapter_md_path(1)
    _ch.parent.mkdir(parents=True, exist_ok=True)
    _ch.write_text("主角在市集。\n", encoding="utf-8")
    assert verify_ledger(store)["consistent"] is True

    line = store.events_path.read_text(encoding="utf-8").splitlines()[0]
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

    result = verify_ledger(store)
    assert result["consistent"] is False
    assert result["duplicate_chapter_events"] == [1]
    assert any("duplicate_chapter_events" in d for d in result["diffs"])
    assert "entities" not in result["diffs"]  # 字段比对确实发现不了


def test_verify_detects_missing_chapter_file(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    commit_event(store, 1, {"moves": [{"who": "主角", "to": "市集"}], "named": ["主角"]})
    result = verify_ledger(store)
    assert result["consistent"] is False
    assert result["missing_chapter_files"] == [1]

    _ch = store.chapter_md_path(1)
    _ch.parent.mkdir(parents=True, exist_ok=True)
    _ch.write_text("主角在市集。\n", encoding="utf-8")
    assert verify_ledger(store)["consistent"] is True


def test_ledger_repair_rebuilds_tampered_snapshot(tmp_path: Path, capsys):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    commit_event(
        store,
        1,
        {"moves": [{"who": "主角", "to": "市集"}], "facts": [{"who": "主角", "text": "拒收改期"}],
         "named": ["主角"]},
    )
    _ch = store.chapter_md_path(1)
    _ch.parent.mkdir(parents=True, exist_ok=True)
    _ch.write_text("主角在市集。\n", encoding="utf-8")

    snap = read_json(store.snapshot_path)
    snap["entities"]["主角"]["location"] = "被篡改的地点"
    snap["debts"] = [{"id": "凭空多出的债", "who": "主角", "status": "open"}]
    atomic_json(store.snapshot_path, snap)
    assert verify_ledger(store)["consistent"] is False

    code = main(["ledger", "repair", "--from-events", "--project", str(proj)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["repaired"] is True
    assert set(payload["changed_fields"]) >= {"entities", "debts"}
    assert verify_ledger(store)["consistent"] is True
    assert read_json(store.snapshot_path)["entities"]["主角"]["location"] == "市集"


def test_ledger_repair_is_idempotent_on_healthy_ledger(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    commit_event(store, 1, {"moves": [{"who": "主角", "to": "市集"}], "named": ["主角"]})
    _ch = store.chapter_md_path(1)
    _ch.parent.mkdir(parents=True, exist_ok=True)
    _ch.write_text("主角在市集。\n", encoding="utf-8")
    result = repair_ledger(store)
    assert result["repaired"] is False
    assert result["changed_fields"] == []
    assert verify_ledger(store)["consistent"] is True


def test_rollback_ledger_to_keeps_earlier_chapters(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    commit_event(store, 1, {"facts": [{"who": "主角", "text": "第一章事实"}], "named": ["主角"]})
    commit_event(store, 2, {"facts": [{"who": "主角", "text": "第二章事实"}], "named": ["主角"]})
    assert max_event_chapter(store) == 2

    info = rollback_ledger_to(store, 2)
    assert info["removed_events"] == 1
    assert info["kept_events"] == 1
    assert max_event_chapter(store) == 1
    assert _facts_of(store, "主角") == ["第一章事实"]
    assert read_json(store.snapshot_path)["chapter"] == 1


def test_write_command_refuses_when_locked(tmp_path: Path, capsys):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    with store.exclusive_lock():
        code = main(["chapter", "next", "--project", str(proj)])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error"]["code"] == "locked"
    assert payload["error"]["details"]["holder"]
    # 锁释放后同一命令即可正常执行
    assert main(["chapter", "next", "--project", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "draft"


def test_read_only_commands_ignore_the_lock(tmp_path: Path, capsys):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    with store.exclusive_lock():
        assert main(["status", "--project", str(proj)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert main(["ledger", "verify", "--project", str(proj)]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True


def test_lock_file_lives_under_run_dir(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    with store.exclusive_lock() as lock_path:
        assert lock_path == store.run_dir / "LOCK"
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder["pid"] > 0
        assert holder["ts"] > 0


@pytest.mark.parametrize("chapter", [1, 2])
def test_rollback_is_noop_when_ledger_has_nothing(tmp_path: Path, chapter: int):
    proj = _make_project(tmp_path, name=f"empty{chapter}")
    store = BookStore(proj)
    info = rollback_ledger_to(store, chapter)
    assert info["removed_events"] == 0
    assert read_json(store.snapshot_path)["entities"] == {}
