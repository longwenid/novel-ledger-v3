"""第三轮质检修复的回归测试：资产不漂移、写路守卫、死亡三态。

这一批的共同形态是「文档/设计声称有，机器通道却没有」——它们不会让测试转红，
只会让长篇在几十章之后开始漂。每一条对应一处已复现的缺陷：

- 死者以遗体/鬼魂/闪回出场被硬拦，而 pack 一直告诉写者这是允许的（缺声明通道）；
- `dead` 只能置位不能清除，复活/夺舍结构上做不了；
- 完结的书仍能被 `plan extend` / `kb sync` / `voice apply` 改故事状态；
- 同一件道具换个写法（只给 name / 只改 status）就长出第二条或继续被当成在持；
- `moves` 缺 `to` 静默把角色 location 清空；
- `resync-baseline` 写了没人读的 `words`，进度读数不刷新；
- `submit` 路径的否定式 voice 笔记被静默吞掉；
- 章拍性格可无声换芯（值域与基线都没人管）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.content.gates import _delta_issues, validate_write_output
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    book_close_early,
    resync_baseline,
    status,
    validate_plan,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json
from novel_ledger_core.ledger.ledger import (
    EMPTY_SNAPSHOT,
    apply_delta_conflicts,
    apply_event,
    select_items,
)
from tests.decoupled_helpers import make_chapter, make_plan, write_plan

PROSE = (
    "主角站在市集当中，掌柜把账本往柜台上一压。"
    "主角当场拒收改期，凭据还压在柜台那头。"
    "两人隔着柜台来回说了几句，谁也没有让步，改天还得再来取。"
)


def _plan() -> dict:
    return make_plan(
        [
            make_chapter(
                1,
                volume=1,
                beats=[
                    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": "凭据仍被扣", "must": "凭据"},
                ],
            ),
            make_chapter(
                2,
                volume=1,
                beats=[
                    {"id": "b1", "required": True, "text": "主角再访市集", "must": "再访"},
                    {"id": "b2", "required": True, "text": "凭据仍未归还", "must": "凭据"},
                ],
            ),
        ]
    )


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    plan_path = write_plan(tmp_path, _plan())
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    return proj


def _write_plan(store: BookStore, plan: dict) -> None:
    store.plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 死亡三态：nonliving 声明出口 + revivals 撤销
# --------------------------------------------------------------------------- #


def _dead_snapshot() -> dict:
    snap = dict(EMPTY_SNAPSHOT)
    return apply_event(
        snap,
        {"chapter": 1, "state_delta": {"named": ["掌柜"], "deaths": [{"who": "掌柜"}]}},
    )


def test_dead_character_still_blocks_undeclared_presence():
    """基线不能松：没声明非活人在场时，死者出现在 named/moves 仍要停线。"""
    snap = _dead_snapshot()
    assert apply_delta_conflicts(snap, {"named": ["掌柜"]}) == [
        {"code": "dead_speaking", "who": "掌柜"}
    ]
    assert apply_delta_conflicts(snap, {"moves": [{"who": "掌柜", "to": "义庄"}]}) == [
        {"code": "dead_speaking", "who": "掌柜"}
    ]


def test_nonliving_declaration_opens_the_flashback_path():
    """pack 一直允许"遗体/遗物/鬼魂/闪回"，delta 却早前没有通道声张——现在有了。"""
    snap = _dead_snapshot()
    declared = {
        "named": ["掌柜"],
        "moves": [{"who": "掌柜", "to": "义庄"}],
        "nonliving": ["掌柜"],
    }
    assert apply_delta_conflicts(snap, declared) == []


def test_revivals_clears_the_dead_flag():
    """`dead` 早前只能置位不能清除，复活/夺舍在结构上做不了。"""
    snap = _dead_snapshot()
    assert snap["entities"]["掌柜"]["dead"] is True

    revived = apply_event(
        snap, {"chapter": 5, "state_delta": {"revivals": ["掌柜"], "named": ["掌柜"]}}
    )
    assert revived["entities"]["掌柜"]["dead"] is False
    assert revived["entities"]["掌柜"]["revived_chapter"] == 5
    assert apply_delta_conflicts(revived, {"named": ["掌柜"]}) == []


def test_death_and_revival_keys_are_type_checked():
    pack = {"present_cards": [{"name": "主角", "is_first_appearance": False}]}
    for key in ("nonliving", "revivals"):
        issues = _delta_issues({key: "not-a-list"}, pack)
        assert {"code": f"state_delta_{key}_not_list", "field": key} in issues


# --------------------------------------------------------------------------- #
# 写路守卫：完结的书不接受改故事状态的写命令
# --------------------------------------------------------------------------- #


def test_story_state_writes_refuse_a_completed_book(tmp_path: Path, capsys):
    """逐个命令补守卫是本仓旧病——kb sync / voice apply / plan extend 全漏过。

    这些命令改的正是 pack 的输入指纹与账本，封笔后无法再通过任何章节被记录或消解。
    出口是 `book reopen`，不是手改 HEAD.json。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    book_close_early(store, actor="作者", reason="测试提前封笔", author_confirmed=True)

    more = tmp_path / "more.json"
    more.write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "chapter": 3,
                        "volume": 1,
                        "location": "市集",
                        "present": ["主角", "掌柜"],
                        "beats": [
                            {"id": "b1", "required": True, "text": "第三章再访", "must": "再访"},
                            {"id": "b2", "required": True, "text": "凭据归还", "must": "凭据"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    blocked = [
        ["plan", "extend", "--project", str(proj), "--chapters", str(more)],
        ["kb", "sync", "--project", str(proj)],
        ["voice", "apply", "--project", str(proj), "--voice", "cinematic"],
        ["hooks", "close", "--project", str(proj), "--id", "h1", "--reason", "r"],
        ["chapter", "patch", "--project", str(proj), "--chapter", "1", "--target", "无", "--replacement", "有"],
    ]
    for argv in blocked:
        code = main(argv)
        payload = json.loads(capsys.readouterr().out)
        assert code == 1, argv
        assert payload["error"]["code"] == "book_completed", argv

    # 计划确实没被改长——守卫是真的挡住了写，而不是只在响应里报错。
    assert [c["chapter"] for c in read_json(store.plan_path)["chapters"]] == [1, 2]


def test_reopen_restores_the_story_write_path(tmp_path: Path, capsys):
    """守卫必须可逆：`book reopen` 之后同样的命令要能跑。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    book_close_early(store, actor="作者", reason="测试提前封笔", author_confirmed=True)

    assert main(["book", "reopen", "--project", str(proj), "--actor", "作者", "--reason", "继续写"]) == 0
    capsys.readouterr()
    assert store.read_head()["status"] == "active"
    (store.canon_dir / "规则.md").write_text(
        "# 规则\n\n【硬】城门日落即闭。\n",
        encoding="utf-8",
    )
    assert main(["kb", "sync", "--project", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_book_reopen_takes_the_write_lock(tmp_path: Path):
    """reopen 也写 HEAD：漏了它就有第二个不加锁的写者。"""
    from novel_ledger_core.control.cli import _needs_write_lock
    from novel_ledger_core.control import cli as cli_mod

    parser = cli_mod.build_parser() if hasattr(cli_mod, "build_parser") else None
    if parser is None:  # 无公开 build_parser 时直接构造命名空间
        import argparse

        args = argparse.Namespace(cmd="book", book_cmd="reopen")
    else:
        args = parser.parse_args(["book", "reopen", "--actor", "a", "--reason", "r"])
    assert _needs_write_lock(args) is True


# --------------------------------------------------------------------------- #
# 道具账本：合并键与"在持"语义
# --------------------------------------------------------------------------- #


def test_item_matched_by_name_instead_of_growing_a_ghost_copy():
    """同一柄剑第一次带 id、第二次只给 name，早前会被当成两件。"""
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {"chapter": 1, "state_delta": {"items": [{"id": "item_jian", "name": "青铜残剑", "holder": "主角"}]}},
    )
    snap = apply_event(
        snap,
        {"chapter": 5, "state_delta": {"items": [{"name": "青铜残剑", "holder": "掌柜", "status": "transferred"}]}},
    )
    assert len(snap["items"]) == 1
    assert snap["items"][0]["holder"] == "掌柜"
    assert snap["items"][0]["status"] == "held"


def test_distinct_items_sharing_a_display_name_are_not_merged():
    """两个都有显式 id 时按 id 分辨——同名不该被强并成一条。"""
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {
            "chapter": 1,
            "state_delta": {
                "items": [
                    {"id": "ling_shi_a", "name": "灵石", "holder": "主角"},
                    {"id": "ling_shi_b", "name": "灵石", "holder": "掌柜"},
                ]
            },
        },
    )
    assert len(snap["items"]) == 2


def test_consumed_and_transferred_items_are_not_held_assets():
    """未确认旧→新持有人的 transferred 与已消耗物品不可作为在持资产。"""
    for status, quantity in (("used", 1), ("transferred", 1), ("lost", 1), ("held", 0)):
        snap = dict(EMPTY_SNAPSHOT)
        snap = apply_event(
            snap,
            {
                "chapter": 1,
                "state_delta": {
                    "items": [
                        {"id": "sword", "name": "剑", "holder": "主角", "status": status, "quantity": quantity}
                    ]
                },
            },
        )
        assert select_items(None, names=["主角"], cap=8, snapshot=snap)[0] == [], (status, quantity)


def test_damaged_item_is_still_held():
    """`damaged` 仍在该人手上，只是带伤——不能一并排掉。"""
    snap = dict(EMPTY_SNAPSHOT)
    snap = apply_event(
        snap,
        {"chapter": 1, "state_delta": {"items": [{"id": "sword", "name": "剑", "holder": "主角", "status": "damaged"}]}},
    )
    assert [i["id"] for i in select_items(None, names=["主角"], cap=8, snapshot=snap)[0]] == ["sword"]


# --------------------------------------------------------------------------- #
# moves 必须给目的地：缺了会静默清空 location
# --------------------------------------------------------------------------- #


def test_move_without_destination_is_rejected():
    """文档把 `to` 列为必填，而缺它的 move 会把角色 location 清成空串、从 occupancy 消失。"""
    pack = {"present_cards": [{"name": "主角", "is_first_appearance": False}], "now_card": {"name": "主角"}}
    issues = _delta_issues({"moves": [{"who": "主角"}]}, pack)
    assert any(i["code"] == "move_missing_to" for i in issues)


def test_move_with_destination_still_passes():
    pack = {"present_cards": [{"name": "主角", "is_first_appearance": False}], "now_card": {"name": "主角"}}
    assert not [i for i in _delta_issues({"moves": [{"who": "主角", "to": "市集"}]}, pack) if i["code"].startswith("move_")]


# --------------------------------------------------------------------------- #
# resync-baseline 必须刷新进度读数
# --------------------------------------------------------------------------- #


def test_resync_baseline_refreshes_word_count(tmp_path: Path):
    """`resync` 早前写 `meta["words"]`，而所有读者用 `word_count`——进度读数不刷新。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_md = store.chapter_md_path(1)
    chapter_md.parent.mkdir(parents=True, exist_ok=True)
    chapter_md.write_text(PROSE, encoding="utf-8")
    # 模拟外部手改后 meta 落后（resync 的正常使用场景）
    store.meta_path(1).write_text(
        json.dumps({"chapter": 1, "word_count": 0}, ensure_ascii=False), encoding="utf-8"
    )
    assert status(store)["book_words_written"] == 0

    resync_baseline(store)
    assert status(store)["book_words_written"] > 0


# --------------------------------------------------------------------------- #
# submit 路径的 voice 笔记不允许静默丢弃
# --------------------------------------------------------------------------- #


def test_submit_reports_dropped_voice_notes(tmp_path: Path):
    """`ack --voice-note` 已会回执，写者 `memory.voice_concepts` 这条路却仍静默吞。"""
    from tests.decoupled_helpers import advance_to_assembly

    proj = _project(tmp_path)
    store = BookStore(proj)
    from novel_ledger_core.control.pipeline import submit_output

    advance_to_assembly(store, prose=PROSE, chapter=1)
    pack = read_json(store.assemble_pack_path(1))
    payload = submit_output(
        store,
        {
            "prose": PROSE,
            "l1_summary": "主角拒收改期，凭据仍被扣。",
            "state_delta": {
                "moves": [],
                "facts": [{"who": "主角", "text": "主角拒收改期，凭据仍被扣。"}],
                "debts": [],
                "hooks": [],
                "relations": [],
                "named": ["主角", "掌柜"],
                "new_names": [],
                "deaths": [],
            },
            "memory": {"voice_concepts": ["对白再磕一点", "不要写这种顺口对白"]},
            "pack_hash": pack["pack_hash"],
            "beats_hit": ["b1", "b2"],
        },
    )
    assert payload["verdict"] == "accepted", payload
    # 入账发生在紧接着的 `chapter next`（_commit），回执也随那次响应返回。
    from novel_ledger_core.control.pipeline import chapter_next

    committed = chapter_next(store)
    assert committed["action"] == "ack", committed
    dropped = committed.get("voice_note_dropped")
    assert dropped, committed
    assert dropped["notes"] == ["不要写这种顺口对白"]


# --------------------------------------------------------------------------- #
# 章拍性格：值域与跨章基线
# --------------------------------------------------------------------------- #





# --------------------------------------------------------------------------- #
# 组装阶段契约：卡片与代码不许各说各话
# --------------------------------------------------------------------------- #


def test_assemble_role_card_states_the_real_required_key_count():
    """v2 adds mandatory plot_findings without restoring prose as model output."""
    from novel_ledger_core.content.gates import WRITE_KEYS

    root = Path(__file__).resolve().parents[2]
    card = (root / "agents" / "roles" / "ledger-editor.md").read_text(encoding="utf-8")
    assert f"{len(WRITE_KEYS) + 1} 大顶层 Key" in card
    for key in (*WRITE_KEYS, "plot_findings"):
        assert key in card
    assert "assembly_prose_mismatch" not in card

    prompts = (root / "references" / "dispatch.md").read_text(encoding="utf-8")
    assert f"{len(WRITE_KEYS) + 1} 个顶层 key" in prompts
