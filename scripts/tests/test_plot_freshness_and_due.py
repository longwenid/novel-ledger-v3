"""sess a4698168（90年代探案2：ch64「笔锋」死锁 + due 毒化）缺陷修复回归。

三笔缺陷同源是「一次性判断/毒值没有新鲜度与类型防线」：
1. plot_findings 是组装 worker 对其所读正文的一次性判断；prose 返工后旧 BLOCKER
   被 submit 无限回收（补了锚词仍报「全篇未出现该词」，回执 quote 里就带着该词）
   → 组装视图烙 prose_hash 凭证，submit 比对不符即拒收；beat 锚词类发现另加
   确定性复检兜底（现行正文已兑现的过期 BLOCKER 就地清除留痕）。
2. 旧 delta_schema 把 debts.due 文档化成「int 或 '第 N 章' 文本」→ 毒值入账后
   chapter next 的到期检查裸 int() 崩（实测宿主手改 events/snapshot 七轮）
   → 契约改正 + 提交端拒收 + 合并归一 + 读点铠甲。
3. submit 的 blocked 恢复 hint 把 draft-submit 排在 chapter next 前面（后者从
   blocked 必被拒，宿主照做白吃一轮 wrong_phase）→ 顺序改正。
"""

from __future__ import annotations

import json
from pathlib import Path

from novel_ledger_core.content.gates import recheck_beat_anchor_blockers
from novel_ledger_core.control.pipeline import (
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import atomic_json, read_json, sha256_text
from novel_ledger_core.ledger.ledger import coerce_due, repair_ledger

from scripts.tests.test_decoupled_pipeline import (
    _PLACEHOLDER,
    _assemble_output,
    _drive_to_assembly,
    _polished,
    _project,
)


def _current_prose_hash(store: BookStore, chapter: int = 1) -> str:
    return "sha256:" + sha256_text(_polished(store, chapter).rstrip("\n"))


def test_assemble_view_and_brief_carry_prose_hash(tmp_path: Path):
    """组装视图/简报烙现行终稿哈希：worker 模板预填，逐字回显即凭证。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    view = read_json(store.assemble_pack_path(1))
    assert view["prose_hash"] == _current_prose_hash(store)
    brief = store.assemble_brief_path(1).read_text(encoding="utf-8")
    assert view["prose_hash"] in brief
    assert '"prose_hash"' in brief
    assert "beat_anchor_missing" in brief  # beat_id 申报口径随模板下发


def test_stale_prose_hash_refuses_submission(tmp_path: Path):
    """prose_hash 与现行终稿不符 → 拒收指路重跑组装；不动相位、不耗配额。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["prose_hash"] = "sha256:" + "0" * 64
    res = submit_output(store, output)
    assert res["verdict"] == "stale_plot_findings"
    assert res["phase"] == "await_assembly"
    assert res["declared_prose_hash"] != res["current_prose_hash"]
    assert "assemble" in res["hint"]
    head = store.read_head()
    assert head["phase"] == "await_assembly"
    assert int(head.get("rewrite_count") or 0) == 0


def test_matching_hash_with_stale_beat_blocker_is_deterministically_cleared(tmp_path: Path):
    """复现 ch64 死锁形态：正文已补锚词，组装件还带着过期 beat 锚词 BLOCKER。

    凭证匹配（worker 确实读的是现行正文……的旧判断被宿主补词后原样重提时凭证
    也被同步）→ 确定性复检通过，过期发现清除留痕，submit 放行。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    polished = _polished(store)
    output = _assemble_output(store, polished)
    output["prose_hash"] = _current_prose_hash(store)
    quote = polished[10:30]  # 真实连续子串，过 quote-in-prose 机检
    output["plot_findings"] = [
        {
            "code": "beat_anchor_missing",
            "beat_id": "b1",
            "severity": "BLOCKER",
            "hint": "第1场硬锚词「拒收」未在终稿出现",
            "quote": quote,
        }
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "accepted", res
    codes = [w.get("code") for w in res.get("warnings") or []]
    assert "stale_plot_finding_cleared" in codes
    # 本该阻塞的发现被清除而不是静默：quality 日志留痕
    quality = store.quality_log_path.read_text(encoding="utf-8")
    assert "plot_self_check_recheck" in quality


def test_stale_beat_blocker_still_blocks_when_anchor_truly_missing(tmp_path: Path):
    """锚词真缺 → 复检不过，BLOCKER 照常回草稿（复检不豁免真缺陷）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    # 终稿不含「拒收」：重写占位正文，只保留 b2 的「凭据」
    prose = "主角在市集徘徊。凭据还压在柜台那头，改天再来取。"
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(prose, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(prose, encoding="utf-8")
    stage_polish_submit(store)
    r3 = chapter_next(store)
    assert r3["action"] == "assemble"
    output = _assemble_output(store, prose)
    output["beats_hit"] = ["b1", "b2"]
    res = submit_output(store, output)
    # 拍点机检先拦（beat_token_missing），轮不到复检豁免
    assert res["verdict"] in ("rewrite", "blocked")
    codes = {v.get("code") for v in res.get("violations") or []}
    assert "beat_token_missing" in codes


def test_recheck_helper_conservative_on_non_beat_blockers(tmp_path: Path):
    """非锚词类 BLOCKER 不可机械复核：复检只清锚词类，其余原样保留。"""
    pack = {"beats": [{"id": "b1", "required": True, "must": "拒收"}]}
    prose = "主角拒收改期。"
    blockers = [
        {"code": "beat_anchor_missing", "severity": "BLOCKER", "hint": "x", "quote": "主角拒收改期"},
        {"code": "dead_speaking", "severity": "BLOCKER", "hint": "y", "quote": "主角拒收改期"},
    ]
    remaining, cleared = recheck_beat_anchor_blockers(pack, prose, blockers)
    assert [f["code"] for f in remaining] == ["dead_speaking"]
    assert [f["code"] for f in cleared] == ["beat_anchor_missing"]


def test_hook_due_text_rejected_at_submit(tmp_path: Path):
    """due:"第 36 章" 在提交端即拒（hook_due_not_int），毒值不得入账。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["state_delta"]["hooks"] = [
        {"id": "hook-x", "text": "凭据下章再取", "status": "open", "due": "第 36 章"}
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "fix_assembly"
    codes = {v.get("code") for v in res.get("violations") or []}
    assert "hook_due_not_int" in codes


def test_debt_due_text_rejected_at_submit(tmp_path: Path):
    """debts.due 同规：旧契约文档化的「第 N 章」文本现在拒收。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["state_delta"]["debts"] = [
        {"id": "debt-x", "who": "掌柜", "text": "欠一个答复", "status": "open", "due": "第 9 章"}
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "fix_assembly"
    codes = {v.get("code") for v in res.get("violations") or []}
    assert "debt_due_not_int" in codes


def test_digit_string_due_is_accepted_and_coerced(tmp_path: Path):
    """宽容口径：数字字符串 "36" 合法，入账归一成 int 36。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["state_delta"]["hooks"] = [
        {"id": "hook-y", "text": "凭据下章再取", "status": "open", "due": "36"}
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "accepted", res
    assert chapter_next(store)["action"] == "ack"  # commit 在 ack 消费时落账
    hooks = {h["id"]: h for h in read_json(store.snapshot_path).get("hooks") or []}
    assert hooks["hook-y"]["due"] == 36
    assert isinstance(hooks["hook-y"]["due"], int)


def test_poisoned_snapshot_due_does_not_crash_chapter_next(tmp_path: Path):
    """存量毒账兜底：snapshot 里 due:"第 36 章" 不再炸 chapter next。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    assert submit_output(store, output)["verdict"] == "accepted"
    snap = read_json(store.snapshot_path)
    snap["hooks"] = [
        {"id": "hook-c34", "text": "矮墙蹲守", "status": "open", "due": "第 36 章"}
    ]
    atomic_json(store.snapshot_path, snap)
    # chapter next（无论消费 ack 还是派下一阶段）都要跑 due 到期扫描：不崩即铠甲生效
    r = chapter_next(store)
    assert r.get("action") in ("ack", "draft")


def test_ledger_repair_washes_due_poison_from_snapshot(tmp_path: Path):
    """repair --from-events 按真源重放：snapshot 里的毒条目一轮洗净。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    assert submit_output(store, output)["verdict"] == "accepted"
    snap = read_json(store.snapshot_path)
    snap["hooks"] = [
        {"id": "hook-c34", "text": "矮墙蹲守", "status": "open", "due": "第 36 章"}
    ]
    atomic_json(store.snapshot_path, snap)
    res = repair_ledger(store)
    assert res["repaired"] is True
    hooks = {h["id"]: h for h in read_json(store.snapshot_path).get("hooks") or []}
    assert "hook-c34" not in hooks  # 真源里从没这条：伪条目随重放消失


def test_apply_event_coerces_text_due_on_merge():
    """合并归一：delta hooks/debts 的「第 N 章」文本 due 入快照即成 int（存量毒账靠 repair 重放洗净）。"""
    from novel_ledger_core.ledger.ledger import apply_event, EMPTY_SNAPSHOT

    snap = dict(EMPTY_SNAPSHOT)
    snap["hooks"] = []
    snap["debts"] = []
    updated = apply_event(
        snap,
        {
            "type": "chapter",
            "chapter": 1,
            "state_delta": {
                "hooks": [{"id": "h1", "text": "矮墙蹲守", "status": "open", "due": "第 36 章"}],
                "debts": [{"id": "d1", "who": "掌柜", "text": "欠答复", "status": "open", "due": "9"}],
            },
        },
    )
    hooks = {h["id"]: h for h in updated["hooks"]}
    debts = {d["id"]: d for d in updated["debts"]}
    assert hooks["h1"]["due"] == 36 and isinstance(hooks["h1"]["due"], int)
    assert debts["d1"]["due"] == 9 and isinstance(debts["d1"]["due"], int)
    # 判读不出数字 → 剥掉 due 键（缺 due = 无限期），不留毒
    updated2 = apply_event(
        dict(EMPTY_SNAPSHOT),
        {
            "type": "chapter",
            "chapter": 1,
            "state_delta": {"hooks": [{"id": "h2", "text": "x", "status": "open", "due": "下次再收"}]},
        },
    )
    hook2 = updated2["hooks"][0]
    assert "due" not in hook2


def test_block_recovery_hints_prescribe_chapter_next_first(tmp_path: Path):
    """blocked 恢复顺序：先 chapter next（blocked→await_draft），再 draft-submit。

    实测旧 hint 让宿主先 draft-submit，从 blocked 必吃 wrong_phase 白费一轮。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    # 制造 rewrite 判决后把它推成 blocked：先吃一次 plot_fix（rewrite_count=1），
    # 再提交同样过期发现 → rewrite 配额尽 → blocked。
    quote = _polished(store)[10:30]
    output["plot_findings"] = [
        {"code": "beat_missing", "severity": "BLOCKER", "hint": "凭据场景没兑现", "quote": quote}
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "plot_fix"
    # plot_fix 收口删了组装件与终稿：重走 draft→polish→assemble
    _drive_to_assembly(store, _PLACEHOLDER)
    output2 = _assemble_output(store, _polished(store))
    output2["plot_findings"] = [
        {"code": "beat_missing", "severity": "BLOCKER", "hint": "凭据场景没兑现", "quote": quote}
    ]
    blocked = submit_output(store, output2)
    assert blocked["verdict"] == "blocked"
    # 从 blocked 重提组装 → wrong_phase，hint 必须教「chapter next FIRST」
    from novel_ledger_core.infra.util import LedgerError

    try:
        submit_output(store, output2)
        raise AssertionError("submit from blocked should refuse")
    except LedgerError as exc:
        assert exc.code == "wrong_phase"
        hint = str((exc.details or {}).get("hint") or "")
        assert hint.index("chapter next") < hint.index("draft-submit")


def test_draft_submit_from_blocked_names_chapter_next(tmp_path: Path):
    """draft-submit 从 blocked 被拒时，错误回执必须点名先跑 chapter next。"""
    from novel_ledger_core.control.pipeline import stage_draft_submit
    from novel_ledger_core.infra.util import LedgerError

    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    quote = _polished(store)[10:30]
    output["plot_findings"] = [
        {"code": "beat_missing", "severity": "BLOCKER", "hint": "x", "quote": quote}
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "plot_fix"  # HEAD 已回 await_draft，不是 blocked；
    # 直接构造 blocked：把 head 拨到 blocked 模拟 ledger_conflict 停线
    head = store.read_head()
    head["phase"] = "blocked"
    head["blocked"] = {"chapter": 1, "reason": "ledger_conflict", "violations": []}
    store.write_head(head)
    try:
        stage_draft_submit(store)
        raise AssertionError("draft-submit from blocked should refuse")
    except LedgerError as exc:
        assert exc.code == "wrong_phase"
        hint = str((exc.details or {}).get("hint") or "")
        assert "chapter next" in hint


def test_coerce_due_semantics():
    assert coerce_due("第 36 章") == 36
    assert coerce_due("36") == 36
    assert coerce_due(36) == 36
    assert coerce_due(36.0) == 36
    assert coerce_due(None) is None
    assert coerce_due("无限期") is None
    assert coerce_due(True) is None


def test_hook_defer_with_text_due_is_coerced_or_diagnosed(tmp_path: Path):
    """hook.defer 是宿主修复通道：数字文本 due 宽容归一；完全判读不出才拒（invalid_new_due）。"""
    from novel_ledger_core.infra.util import LedgerError
    from novel_ledger_core.ledger.ledger import append_governance_event

    proj = _project(tmp_path)
    store = BookStore(proj)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["state_delta"]["hooks"] = [
        {"id": "hook-z", "text": "矮墙蹲守", "status": "open", "due": 30}
    ]
    assert submit_output(store, output)["verdict"] == "accepted"
    assert chapter_next(store)["action"] == "ack"  # 先落账，hook-z 才进快照
    # 「第 40 章」可判读 → 归一成 40，不拒（宿主修复通道宁宽容）
    append_governance_event(
        store,
        action="hook.defer",
        actor="managing-editor",
        reason="改期",
        fields={"hook_id": "hook-z", "new_due": "第 40 章"},
    )
    hooks = {h["id"]: h for h in read_json(store.snapshot_path).get("hooks") or []}
    assert hooks["hook-z"]["due"] == 40
    # 无数字 → invalid_new_due 诊断回执，不再裸 int 崩
    try:
        append_governance_event(
            store,
            action="hook.defer",
            actor="managing-editor",
            reason="改期",
            fields={"hook_id": "hook-z", "new_due": "以后再说"},
        )
        raise AssertionError("hook.defer with undecipherable due should refuse")
    except LedgerError as exc:
        assert exc.code == "invalid_new_due"


def test_delta_schema_no_longer_documents_text_due():
    """契约源头：delta_schema 不再文档化「int 或 '第 N 章' 文本」这种合法形态。"""
    from novel_ledger_core.content.pack import _WRITE_CONTRACT

    schema = json.dumps(_WRITE_CONTRACT["delta_schema"], ensure_ascii=False)
    assert "int 或 '第 N 章'" not in schema
    assert "不收「第 N 章」文本" in schema
