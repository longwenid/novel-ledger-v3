from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (ack_read, book_close_early, book_complete, book_reopen,
    chapter_next, stage_draft_submit, stage_polish_submit, submit_output)
from novel_ledger_core.control.cli import main
from novel_ledger_core.content.story_review import (pending_review, prepare_review, submit_review,
    receipts, export_evidence, REVIEW_EVIDENCE_DIMENSIONS)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json, sha256_text

PROSE = "主角追查母亲留下的遗书，决定放弃奖金去救掌柜。掌柜交出落款，旧案终于有了证据。\n\n主角带着遗书离开试炼场，记得自己付出的代价，也明白旧日承诺必须兑现。"


def story_store(tmp_path: Path, *, chapters: int = 3, split: bool = True) -> BookStore:
    plan = {"protagonist": "主角", "chapters": [
        {"chapter": n, "volume": 1 if not split or n <= 2 else 2, "location": "试炼场", "present": ["主角", "掌柜"],
         "beats": [{"id": "b1", "required": True, "must": "遗书", "text": "找回遗书"}]}
        for n in range(1, chapters + 1)],
        "volumes": {"vol-0001": {"spine": "遗书与选择", "goal": "找回母亲遗书"}, "vol-0002": {"spine": "旧案收束", "goal": "兑现承诺"}}}
    source = tmp_path / "plan.json"
    atomic_json(source, plan)
    project = tmp_path / "novel"
    init_project(project, plan_path=source, protagonist="主角", word_min=20, word_max=5000, book_words=100)
    store = BookStore(project)
    cfg = store.load_config()
    cfg.update(execution_mode="stage-agent", review_contract_version=2, plan_low_water=0, style_check="off",
               polish="on")  # 全链泳线测试；skill 默认只写作
    store.save_config(cfg)
    return store


def write_chapter(store: BookStore) -> None:
    action = chapter_next(store)
    assert action["action"] == "draft"
    prose = PROSE + f"\n第{action['chapter']}章的这一笔到此结清。"
    Path(action["draft_output_path"]).write_text(prose, encoding="utf-8")
    stage_draft_submit(store)
    action = chapter_next(store)
    Path(action["polished_output_path"]).write_text(prose, encoding="utf-8")
    stage_polish_submit(store)
    action = chapter_next(store)
    pack = read_json(Path(action["assemble_pack_path"]))
    result = submit_output(store, {"pack_hash": pack["pack_hash"], "l1_summary": "找回遗书，选择救人并承担代价。", "memory": {},
                                   "state_delta": {"named": ["主角", "掌柜"]}, "beats_hit": ["b1"], "plot_findings": []})
    assert result["verdict"] == "accepted"
    assert chapter_next(store)["action"] == "ack"
    ack_read(store, quotes=["主角追查母亲留下的遗书，决定放弃奖金去救掌柜。", "掌柜交出落款，旧案终于有了证据。", "记得自己付出的代价，也明白旧日承诺必须兑现。"])


def review_output(store: BookStore) -> dict:
    view = read_json(store.staging_dir / "story-review-view.json")
    return {"review_id": view["review_id"], "input_hash": view["input_hash"],
            # 显式声明「本次看不到的证据维度」：空表表示逐项都核过了。
            # 漏这个字段会被 story_review_dimensions_missing 拒收（见下面的用例）。
            "unverifiable_dimensions": [],
            "checks": [
        {"id": key, "status": "pass", "reason": "开篇选择、末篇收束留下了实际代价与承诺兑现证据。",
         "evidence": [{"chapter": view["first"], "quote": "决定放弃奖金去救掌柜。"},
                      {"chapter": view["through"], "quote": "也明白旧日承诺必须兑现。"}]}
        for key in view["required_checks"]]}


def pass_pending(store: BookStore, *, completing: bool = False):
    spec = pending_review(store, completing=completing)
    assert spec is not None
    action = prepare_review(store, spec)
    assert submit_review(store, review_output(store))["verdict"] == "pass"
    return action


def test_next_requires_independent_volume_review_before_crossing(tmp_path):
    store = story_store(tmp_path)
    write_chapter(store)
    write_chapter(store)
    action = chapter_next(store)
    assert action["action"] == "story_review"
    assert action["execution"]["context_isolation"] == "fresh_session"
    assert action["execution"]["preferred_tier"] == "strongest"
    assert chapter_next(store)["review_id"] == action["review_id"]
    assert store.read_head()["phase"] == "idle"
    submit_review(store, review_output(store))
    assert chapter_next(store)["action"] == "draft"


@pytest.mark.parametrize("failure", ["empty", "no_quote", "fake_quote", "missing_check", "one_end", "duplicate_check"])
def test_review_rejects_missing_or_fabricated_semantic_evidence(tmp_path, failure):
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    write_chapter(store)
    prepare_review(store, pending_review(store, completing=True))
    output = review_output(store)
    if failure == "empty": output["checks"] = []
    if failure == "no_quote": output["checks"][0]["evidence"] = []
    if failure == "fake_quote": output["checks"][0]["evidence"][0]["quote"] = "正文中从来没有出现过的内容"
    if failure == "missing_check": output["checks"].pop()
    if failure == "one_end":
        for check in output["checks"]: check["evidence"] = check["evidence"][:1]
    if failure == "duplicate_check": output["checks"][-1] = copy.deepcopy(output["checks"][0])
    with pytest.raises(LedgerError):
        submit_review(store, output)
    assert receipts(store) == {}


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("missing", "story_review_dimensions_missing"),
        ("unknown", "story_review_dimensions_unknown"),
        ("pass_with_gaps", "story_review_unverified_dimensions"),
    ],
)
def test_review_must_declare_what_it_could_not_verify(tmp_path, mutation, code):
    """「看不见」要显式报出来：漏声明、瞎声明、判 pass 却留缺口三种都不收。

    跨段实体/数值一致性正是靠「审校没看见却判 pass」漏过去的，所以维度声明是契约而不是建议。
    """
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    write_chapter(store)
    prepare_review(store, pending_review(store, completing=True))
    output = review_output(store)
    if mutation == "missing":
        output.pop("unverifiable_dimensions")
    elif mutation == "unknown":
        output["unverifiable_dimensions"] = ["something_not_declared"]
    else:
        output["unverifiable_dimensions"] = ["timeline"]
    with pytest.raises(LedgerError) as raised:
        submit_review(store, output)
    assert raised.value.code == code
    assert receipts(store) == {}


def test_review_view_carries_evidence_dimensions_and_machine_leads(tmp_path):
    """复核视图要给出：可声明的维度清单 + 机器扫出的待裁决线索（而不是只给结论）。"""
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    write_chapter(store)
    prepare_review(store, pending_review(store, completing=True))
    view = read_json(store.staging_dir / "story-review-view.json")
    assert view["evidence_dimensions"] == list(REVIEW_EVIDENCE_DIMENSIONS)
    leads = view["machine_consistency_hits"]
    assert leads["available"] is True
    assert leads["declared_fact_keys"] == 0  # 未声明取值域 → 只有形态类线索
    assert "unverifiable_dimensions" in view["evidence_note"]


def test_unverifiable_review_stops_then_text_revision_requires_fresh_review(tmp_path):
    store = story_store(tmp_path)
    write_chapter(store)
    write_chapter(store)
    chapter_next(store)
    output = review_output(store)
    output["checks"][0].update(status="UNVERIFIABLE", reason="现有证据不足以证明读者承诺兑现", evidence=[])
    assert submit_review(store, output)["verdict"] == "fix"
    assert chapter_next(store)["action"] == "story_review_blocked"
    # Simulate an acknowledged revision, preserving all original evidence.
    revised = PROSE + "\n遗书交还母亲，旧承诺终于兑现。"
    store.chapter_md_path(2).write_text(revised, encoding="utf-8")
    ack = read_json(store.ack_path(2))
    ack["prose_hash"] = "sha256:" + sha256_text(revised)
    atomic_json(store.ack_path(2), ack)
    assert chapter_next(store)["action"] == "story_review"


def test_receipts_survive_future_chapters_but_bind_contract_and_revision(tmp_path):
    store = story_store(tmp_path)
    write_chapter(store)
    write_chapter(store)
    pass_pending(store)
    assert pending_review(store) is None
    write_chapter(store)
    assert pending_review(store, completing=True)["scope"] == "volume"
    pass_pending(store, completing=True)
    assert pending_review(store, completing=True)["scope"] == "book"
    pass_pending(store, completing=True)
    assert pending_review(store, completing=True) is None
    plan = store.load_plan()
    plan["world_spine"] = "已确认规则修订：遗书不能由同一人拆封。"
    store.save_plan(plan)
    assert pending_review(store, completing=True)["scope"] == "volume"


def test_complete_requires_all_chapters_and_current_volume_endgame_review(tmp_path):
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    with pytest.raises(LedgerError) as exc:
        book_complete(store, actor="作者", reason="正常完本", override_target=True)
    assert exc.value.code == "book_not_finished"
    write_chapter(store)
    with pytest.raises(LedgerError) as exc:
        book_complete(store, actor="作者", reason="正常完本")
    assert exc.value.code == "story_review_required"
    pass_pending(store, completing=True)
    pass_pending(store, completing=True)
    assert book_complete(store, actor="作者", reason="正文与终局复核通过")["action"] == "complete"
    assert store.read_head()["completion_kind"] == "normal"


def test_author_early_closure_is_separate_and_reopen_restores_pending_stage(tmp_path):
    store = story_store(tmp_path)
    chapter_next(store)
    with pytest.raises(LedgerError):
        book_close_early(store, actor="作者", reason="决定封笔", author_confirmed=False)
    result = book_close_early(store, actor="作者", reason="决定封笔", author_confirmed=True)
    assert result["normal_completion"] is False
    assert store.read_head()["completion_kind"] == "early_close"
    assert book_reopen(store, actor="作者", reason="恢复写作")["phase"] == "await_draft"
    assert chapter_next(store)["action"] == "draft"


def test_review_cli_and_bounded_extra_evidence(tmp_path, capsys):
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    write_chapter(store)
    assert main(["review", "story-next", "--complete", "--project", str(store.project)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "story_review"
    assert export_evidence(store, 1)["chars"] == len(store.chapter_md_path(1).read_text(encoding="utf-8"))
    with pytest.raises(LedgerError): export_evidence(store, 3)
    output = store.staging_dir / "story-review-output.json"
    atomic_json(output, review_output(store))
    assert main(["review", "story-submit", "--output", str(output), "--project", str(store.project)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
