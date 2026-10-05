"""运维命令的自动化覆盖：book audit / plan validate / book complete。

这三条命令此前只写在 SKILL.md 的总编辑操作面里，测试套件一处都没调过
（覆盖率实测：`audit_book` 63 条语句、`validate_plan` 50 条语句零覆盖）。
历史 P0-1 的形态与此完全相同——`resync_baseline` 曾是死代码，公开命令崩溃，
直到人工质检才发现。出事时才用的命令，必须平时就有守卫。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    ack_read,
    audit_book,
    book_complete,
    book_close_early,
    chapter_next,
    submit_output,
    validate_plan,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json
from novel_ledger_core.ledger.ledger import commit_event

from tests.decoupled_helpers import advance_to_assembly, make_chapter, make_plan, write_plan

PROSE = (
    "主角站在市集当中，掌柜把账本往柜台上一压。"
    "主角当场拒收改期，凭据还压在柜台那头。"
    "两人隔着柜台来回说了几句，谁也没有让步，改天还得再来取。"
)
QUOTES = [
    "主角站在市集当中，掌柜把账本往柜台上一压",
    "主角当场拒收改期，凭据还压在柜台那头",
    "两人隔着柜台来回说了几句，谁也没有让步，改天还得再来取",
]


def _plan(chapter_count: int = 2) -> dict:
    return make_plan(
        [
            make_chapter(
                i,
                volume=1,
                beats=[
                    {"id": "b1", "required": True, "text": f"第{i}章主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": f"第{i}章凭据仍被扣", "must": "凭据"},
                ],
            )
            for i in range(1, chapter_count + 1)
        ]
    )


def _project(tmp_path: Path, *, chapter_count: int = 2) -> Path:
    plan_path = write_plan(tmp_path, _plan(chapter_count))
    proj = tmp_path / "novel"
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    return proj


def _commit_chapter_one(store: BookStore) -> None:
    """把第 1 章一路走到 ack 通过，留下真实的正文/meta/ack/quality 记录。"""
    advance_to_assembly(store, prose=PROSE, chapter=1)
    pack = read_json(store.assemble_pack_path(1))
    submitted = submit_output(
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
            "memory": {"voice_concepts": []},
            "pack_hash": pack["pack_hash"],
            "beats_hit": ["b1", "b2"],
        },
    )
    assert submitted["verdict"] == "accepted", submitted
    committed = chapter_next(store)
    assert committed["action"] == "ack", committed
    acked = ack_read(store, quotes=list(QUOTES), verdict="pass")
    assert acked["ok"] is True, acked


# --------------------------------------------------------------------------- #
# book audit
# --------------------------------------------------------------------------- #


def test_book_audit_cli_reports_stats_and_quality_summary(tmp_path: Path, capsys):
    """`book audit` 是文档化的总编辑命令，走 CLI 全链也要真的跑出实测结论。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _commit_chapter_one(store)

    code = main(["book", "audit", "--project", str(proj)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["action"] == "book_audit"
    assert payload["stats"]["total_chapters"] == 1
    assert payload["stats"]["total_words"] > 0
    assert payload["hash_mismatch_chapters"] == []
    assert payload["quote_invalid_count"] == 0
    assert payload["ledger_consistent"] is True
    assert payload["quality_summary"]["skipped"] is False
    assert payload["quality_summary"]["begins"] == 1
    assert payload["quality_summary"]["submit_accepted"] == 1


def test_book_audit_flags_hash_mismatch_after_manual_edit(tmp_path: Path):
    """手改正文后 `book audit` 必须报出失配章节（这是它存在的理由）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _commit_chapter_one(store)
    chapter_path = store.chapter_md_path(1)
    chapter_path.write_text(
        chapter_path.read_text(encoding="utf-8").rstrip("\n") + "\n手工加的一句，没有回写 meta。\n",
        encoding="utf-8",
    )

    payload = audit_book(store)
    assert payload["stats"]["total_chapters"] == 1
    assert payload["hash_mismatch_chapters"] == [1]


# --------------------------------------------------------------------------- #
# plan validate
# --------------------------------------------------------------------------- #


def test_plan_validate_passes_on_complete_beats(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)

    payload = validate_plan(store)

    assert payload["action"] == "plan_validate"
    assert payload["error_count"] == 0
    assert payload["passed"] is True


def test_plan_validate_flags_thin_and_incomplete_chapters(tmp_path: Path):
    """编拍缺陷必须在扩写前报出来：空 beats 是错误，缺地点/在场者、must 不落文是警告。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    store.plan_path.write_text(
        json.dumps(
            {
                "protagonist": "主角",
                "chapters": [
                    {"chapter": 1, "location": "", "present": [], "beats": []},
                    {
                        "chapter": 2,
                        "location": "市集",
                        "present": ["主角"],
                        "beats": [{"id": "b1", "required": True, "text": "有事发生", "must": "拒收"}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = validate_plan(store)
    error_codes = {e["code"] for e in payload["errors"]}
    warning_codes = {w["code"] for w in payload["warnings"]}

    assert payload["passed"] is False
    assert "missing_beats" in error_codes
    assert "missing_location" in warning_codes
    assert "missing_present" in warning_codes
    assert "beats_count_off_band" in warning_codes
    assert "must_not_in_beat" in warning_codes


# --------------------------------------------------------------------------- #
# book complete
# --------------------------------------------------------------------------- #


def test_book_complete_closes_the_write_path(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)

    payload = book_close_early(store, actor="作者", reason="本书提前收口", author_confirmed=True)
    assert payload["ok"] is True
    assert payload["phase"] == "complete"
    assert payload["stop"] is True

    follow_up = chapter_next(store)
    assert follow_up["action"] == "complete"
    assert follow_up["stop"] is True


def test_book_complete_reports_signed_long_commitment_until_paid(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    plan = store.load_plan()
    plan["book_outline"] = {"long_term_commitments": [{
        "id": "commitment-seal", "promise": "缺角印信的真相",
        "planted_volume": 1, "resolved_volume": 4,
    }]}
    store.save_plan(plan)
    audit = audit_book(store)
    assert audit["unresolved_long_term_commitments"][0]["hook_status"] == "unplanted"

    commit_event(store, chapter=1, state_delta={"hooks": [{
        "id": "commitment-seal", "text": "印信证实旧令", "status": "paid",
    }]})
    assert audit_book(store)["unresolved_long_term_commitments"] == []
    # 兑现承诺不会把空正文/未通读计划伪装成正常完本。
    with pytest.raises(LedgerError) as rejected:
        book_complete(store, actor="作者", reason="已完成承诺", override_target=True)
    assert rejected.value.code == "book_not_finished"


def test_manual_completion_names_unpaid_long_commitment(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    plan = store.load_plan()
    plan["book_outline"] = {"long_term_commitments": [{
        "id": "commitment-seal", "promise": "缺角印信的真相",
        "planted_volume": 1, "resolved_volume": 4,
    }]}
    store.save_plan(plan)

    # 先构造全部计划已通读的边界，隔离“未兑现承诺”这一拒绝原因。
    head = store.read_head()
    head.update(phase="idle", last_committed_ch=2, last_acked_ch=2)
    store.write_head(head)
    with pytest.raises(LedgerError) as rejected:
        book_complete(store, actor="作者", reason="决定收口", override_target=True)
    assert rejected.value.code == "book_promises_unresolved"
    assert rejected.value.as_dict()["error"]["details"]["commitments"][0]["id"] == "commitment-seal"
    assert store.read_head()["status"] == "active"


def test_book_complete_requires_actor_and_reason(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)

    with pytest.raises(LedgerError) as exc:
        book_complete(store, actor="作者", reason="   ")
    assert exc.value.code == "invalid_actor"


def test_book_complete_blocks_accidental_short_book(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    with pytest.raises(LedgerError) as exc:
        book_complete(store, actor="作者", reason="误以为已经写完")
    assert exc.value.code == "book_target_not_reached"
    assert store.read_head()["status"] == "active"


# --------------------------------------------------------------------------- #
# 配置 schema：升级安全（只写不读的 schema_version 等于没有）
# --------------------------------------------------------------------------- #


def _set_schema_version(store: BookStore, value) -> None:
    cfg = json.loads(store.config_path.read_text(encoding="utf-8"))
    if value is None:
        cfg.pop("schema_version", None)
    else:
        cfg["schema_version"] = value
    store.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def test_future_schema_version_blocks_reads_loudly(tmp_path: Path, capsys):
    """用旧 skill 打开新项目必须当场停线，而不是按旧规则解释新数据。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _set_schema_version(store, 999)

    with pytest.raises(LedgerError) as exc:
        store.load_config()
    assert exc.value.code == "unsupported_schema"

    code = main(["status", "--project", str(proj)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsupported_schema"


def test_config_without_schema_is_rejected(tmp_path: Path):
    """本版本不兼容旧项目，缺少 schema_version 必须直接拒绝。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _set_schema_version(store, None)

    with pytest.raises(LedgerError) as exc:
        store.load_config()
    assert exc.value.code == "invalid_config"


def test_book_reconcile_clean_on_consistent_project(tmp_path: Path):
    """干净项目：book reconcile 各类计数为 0、clean=true。"""
    from novel_ledger_core.control.pipeline import reconcile_book

    proj = _project(tmp_path)
    store = BookStore(proj)
    res = reconcile_book(store)
    assert res["ok"] is True
    assert res["clean"] is True
    assert all(v == 0 for v in res["counts"].values())


def test_book_reconcile_flags_enum_sum_mismatch(tmp_path: Path):
    """正文句内加总写错：reconcile 的 numeric 类命中，并给出受影响章。"""
    from novel_ledger_core.control.pipeline import reconcile_book

    proj = _project(tmp_path)
    store = BookStore(proj)
    _commit_chapter_one(store)
    # 直接在已提交正文里注入算术错（模拟改稿遗留）
    bad = PROSE + "\n铺面月租一百二十文、押两个月二百四十文、中人钱三十六文、验单一百二十文，拢共三百九十六文。\n"
    store.chapter_md_path(1).write_text(bad, encoding="utf-8")
    res = reconcile_book(store)
    assert res["clean"] is False
    codes = {n["code"] for n in res["numeric"]}
    assert "enum_sum_mismatch" in codes
    assert "ch1" in res["chapters_to_fix"]


def test_plan_validate_warns_when_quant_key_missing_from_world_spine(tmp_path: Path):
    """口径键声明了却没进 world_spine（每章 pack 必注入通道）→ 软告警。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["quant_keys"] = ["年租", "中人钱"]
    store.save_config(cfg)

    res = validate_plan(store)
    warns = [w for w in res["warnings"] if w["code"] == "quant_key_not_in_world_spine"]
    assert warns and set(warns[0]["keys"]) == {"年租", "中人钱"}

    # 把口径补进 world_spine 后告警消失
    plan = store.load_plan()
    plan["world_spine"] = "年租按田等；中人钱行规一成至一成五。"
    store.save_plan(plan)
    res2 = validate_plan(store)
    assert not [w for w in res2["warnings"] if w["code"] == "quant_key_not_in_world_spine"]


def test_plan_validate_quant_key_check_is_opt_out_by_default(tmp_path: Path):
    """未声明 quant_keys 的项目不产生该告警（默认零噪声）。"""
    proj = _project(tmp_path)
    res = validate_plan(BookStore(proj))
    assert not [w for w in res["warnings"] if w["code"] == "quant_key_not_in_world_spine"]


def test_resync_baseline_restamps_canon_fingerprint(tmp_path: Path):
    """正典复盘改过后，resync-baseline 重盖章清掉 canon_drift（只改 canon_sha，不动语义）。"""
    from novel_ledger_core.control.pipeline import resync_baseline
    from novel_ledger_core.ledger.ledger import verify_ledger

    proj = _project(tmp_path)
    store = BookStore(proj)
    _commit_chapter_one(store)
    # 复盘后改正典 → 漂移出现
    cards = store.load_kb()
    cards.append({"id": "复盘新增--硬", "kind": "world", "title": "复盘补充", "body": "口径补充。"})
    store.save_kb(cards)
    assert audit_book(store)["canon_drift"]["changed"] is True

    # 默认不动正典指纹：常规哈希对齐不该把"老章节可能按旧正典写"的告警抹掉
    default_res = resync_baseline(store)
    assert default_res["canon_restamped"] is False
    assert audit_book(store)["canon_drift"]["changed"] is True

    # 复盘确认无需回改正文时，显式 opt-in 才重盖章
    res = resync_baseline(store, restamp_canon=True)
    assert res["canon_restamped"] is True
    assert audit_book(store)["canon_drift"]["changed"] is False
    # 显式裁决只追加 canon_sha 基线事件，账本重放仍与快照一致
    assert verify_ledger(store)["consistent"] is True

    # 再跑一次：已对齐，不重复写
    assert resync_baseline(store, restamp_canon=True)["canon_restamped"] is False


def test_audit_probe_flags_summary_naming_an_absent_entity(tmp_path: Path):
    """摘要点到账本里存在、但本章正文没出场的角色，必须报 derived_name_drift。

    历史缺陷：`l1_summary` 是唯一会喂给后续章节的"前情"叙述（near_summaries），
    数字有 derived_numeric_drift 兜底，人物与事实没有——摘要凭空多一个人，
    写者下一章照着写就变成真 canon。
    """
    from novel_ledger_core.control.pipeline import _derived_name_drift
    from novel_ledger_core.ledger.ledger import load_snapshot

    proj = _project(tmp_path)
    store = BookStore(proj)
    _commit_chapter_one(store)
    snap = load_snapshot(store)

    # 第 2 章手写：正文里没有掌柜，摘要却点了他的名
    ch2 = store.chapter_md_path(2)
    ch2.parent.mkdir(parents=True, exist_ok=True)
    ch2.write_text("主角独自赶路，天黑才歇脚。\n", encoding="utf-8")
    summary2 = store.summary_path(2)
    summary2.parent.mkdir(parents=True, exist_ok=True)
    summary2.write_text(
        json.dumps({"chapter": 2, "l1_summary": "主角与掌柜在城外碰头。"}, ensure_ascii=False),
        encoding="utf-8",
    )

    chapters = sorted(store.chapters_dir.glob("*/ch-*.md"))
    hits = _derived_name_drift(store, chapters, snap)
    assert any(h["chapter"] == 2 and "掌柜" in h["names"] for h in hits), hits
    # 第 1 章摘要里的"主角"确实在正文出现过，不该报
    assert not any(h["chapter"] == 1 for h in hits), hits


def test_ops_commands_refuse_uninitialized_project(tmp_path: Path):
    """未初始化项目上跑 verify / repair / hooks audit 必须报 not_initialized。

    历史缺陷：空目录重放得到"零事件、零差异"，verify 返回 consistent=true。
    而 verify 正是崩溃/断电后第一个要跑的命令——一次路径写错（例如把 --project
    指到 book/ 而不是项目根）就被读成"账本没问题"，证据链在这里漏气。
    """
    from novel_ledger_core.control.pipeline import audit_hooks
    from novel_ledger_core.ledger.ledger import repair_ledger, verify_ledger

    store = BookStore(tmp_path / "not-a-project")
    for fn in (verify_ledger, repair_ledger, audit_hooks):
        with pytest.raises(LedgerError) as exc:
            fn(store)
        assert exc.value.code == "not_initialized", fn


def test_book_reopen_reopens_the_write_path(tmp_path: Path):
    """完结要有逃生口：误封笔必须能用命令回到 active，而不是手改 HEAD.json。"""
    from novel_ledger_core.control.pipeline import book_reopen

    proj = _project(tmp_path)
    store = BookStore(proj)

    completed = book_close_early(store, actor="作者", reason="测试提前封笔", author_confirmed=True)
    # 提前收口明确记录为 early_close，不冒充正常验收。
    assert completed["completion_kind"] == "early_close"
    assert store.read_head()["status"] == "completed"

    reopened = book_reopen(store, actor="作者", reason="误封，回来补完")
    assert reopened["ok"] is True
    head = store.read_head()
    assert head["status"] == "active"
    assert head["phase"] == "idle"
    assert chapter_next(store)["ok"] is True

    with pytest.raises(LedgerError) as exc:
        book_reopen(store, actor="作者", reason="重复 reopen")
    assert exc.value.code == "not_completed"
