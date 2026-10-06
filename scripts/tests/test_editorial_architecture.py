from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.autopilot import _checkpoint_report
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    chapter_next, stage_draft_submit, stage_polish_submit, submit_output,
    ack_read, patch_prose, ack_patched_chapter, check_submit_output,
    book_complete,
)
from novel_ledger_core.content.reviews import list_findings, resolve_finding
from novel_ledger_core.infra.util import LedgerError, read_json, chinese_word_count
from tests.test_execution_policy import _store, _assembly_output, PROSE, QUOTES


def isolated_store(tmp_path):
    store = _store(tmp_path)
    config = store.load_config()
    config.update(execution_mode="stage-agent", review_contract_version=2,
                   polish="on")  # 四段泳线架构测试；skill 默认只写作
    store.save_config(config)
    return store


def assembly(store):
    action = chapter_next(store)
    Path(action["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_draft_submit(store)
    action = chapter_next(store)
    Path(action["polished_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_polish_submit(store)
    return chapter_next(store)


def test_review_required_and_unsupported_blocker_stays_in_assembly(tmp_path):
    store = isolated_store(tmp_path)
    assembly(store)
    output = _assembly_output(store)
    result = submit_output(store, output)
    assert result["verdict"] == "fix_assembly"
    assert store.read_head()["rewrite_count"] == 0
    output["plot_findings"] = [{"severity": "BLOCKER", "hint": "无原句的断言"}]
    assert submit_output(store, output)["verdict"] == "fix_assembly"
    assert store.read_head()["rewrite_count"] == 0
    output["plot_findings"] = []
    assert submit_output(store, output)["verdict"] == "accepted"


def test_findings_survive_ack_and_dispositions_are_replayable(tmp_path):
    store = isolated_store(tmp_path)
    assembly(store)
    output = _assembly_output(store)
    output["plot_findings"] = [{"code": "missing_history", "severity": "UNVERIFIABLE", "hint": "需要未入包的旧章认知证据"}]
    # Optional check stays read-only.
    before = sorted(str(p) for p in store.editorial_dir.rglob("*"))
    assert check_submit_output(store, output)["verdict"] == "ready"
    assert sorted(str(p) for p in store.editorial_dir.rglob("*")) == before
    assert submit_output(store, output)["verdict"] == "accepted"
    chapter_next(store)
    ack_read(store, quotes=QUOTES)
    result = list_findings(store)
    assert result["pending_count"] == 1
    finding = result["findings"][0]
    assert finding["code"] == "missing_history"
    checkpoint = _checkpoint_report(store, chapter=1, previous=0, kinds=("batch",))
    assert any(i["code"] == "editorial_findings_unresolved" for i in checkpoint["blockers"])
    resolve_finding(store, finding["id"], disposition="deferred", actor="总编辑", reason="已登记，下批补证")
    assert list_findings(store)["pending_count"] == 0
    assert "已登记，下批补证" in Path(result["journal_path"]).read_text(encoding="utf-8")


def test_patch_preserves_word_band_and_requires_new_review(tmp_path):
    store = isolated_store(tmp_path)
    assembly(store)
    output = {**_assembly_output(store), "plot_findings": []}
    submit_output(store, output); chapter_next(store); ack_read(store, quotes=QUOTES)
    cfg = store.load_config()
    cfg["word_band"] = {"min": chinese_word_count(PROSE) - 1, "max": 5000}
    store.save_config(cfg)
    old = store.chapter_md_path(1).read_text(encoding="utf-8")
    with pytest.raises(LedgerError) as exc:
        patch_prose(store, patches=[("主角仍未离开", "主角未走")], chapter=1)
    assert exc.value.code == "patch_word_band_failed"
    assert store.chapter_md_path(1).read_text(encoding="utf-8") == old
    patch_prose(store, patches=[("主角仍未离开", "主角仍留在门前")], chapter=1)
    assert read_json(store.ack_path(1))["review_status"] == "needs_review"
    with pytest.raises(LedgerError) as exc:
        book_complete(store, actor="作者", reason="收尾", override_target=True)
    assert exc.value.code == "editorial_review_pending"
    with pytest.raises(LedgerError):
        ack_patched_chapter(store, chapter=1, quotes=["不存在的引文" for _ in range(3)])
    ack_patched_chapter(store, chapter=1, quotes=QUOTES)
    assert read_json(store.ack_path(1))["review_status"] == "reviewed"


def test_stage_action_contract_declares_fresh_session(tmp_path):
    """stage-agent 的 execution 信封是宿主契约：fresh_session、空上下文 spawn 指令一次给全。"""
    store = isolated_store(tmp_path)
    action = chapter_next(store)
    assert action["execution"]["context_isolation"] == "fresh_session"
    assert action["execution"]["session_scope"] == "stage"
    assert action["worker"]["context_origin"] == "empty"
    assert action["worker"]["protocol"] == "stage"
    assert "one fresh, empty-context worker" in action["worker"]["spawn"]
    assert Path(action["worker"]["action_path"]).exists()


def test_final_review_unverifiable_is_durable_and_blocker_cannot_pass(tmp_path):
    store = isolated_store(tmp_path)
    assembly(store)
    submit_output(store, {**_assembly_output(store), "plot_findings": []})
    chapter_next(store)
    with pytest.raises(LedgerError) as exc:
        ack_read(store, quotes=QUOTES, findings=[{"severity":"BLOCKER","hint":"客观错误","quote":QUOTES[0]}])
    assert exc.value.code == "ack_verdict_conflict"
    assert store.read_head()["phase"] == "await_ack"
    ack_read(store, quotes=QUOTES, findings=[{"severity":"UNVERIFIABLE","hint":"需要未提供的跨章证据"}])
    assert list_findings(store)["findings"][0]["severity"] == "UNVERIFIABLE"


def test_stage_action_envelope_carries_usage_request_handle(tmp_path):
    """记账句柄随信封走：宿主回声 usage_request.request_id 即天然幂等，
    stage-agent 不再因"信封缺 job_id/session_id"整程 telemetry=unknown。"""
    store = isolated_store(tmp_path)
    action = chapter_next(store)
    request = action["worker"]["usage_request"]
    assert request["request_id"].startswith("draft-ch0001-")
    assert request["shape"] == "components_or_total"
    assert "usage-record" in request["record_hint"]
    envelope = read_json(Path(action["worker"]["action_path"]))
    assert envelope["usage_request"] == request


def test_ack_read_accepts_intuitive_chapter_echo_and_rejects_mismatch(tmp_path, capsys):
    """ack-read 本无章参；实战直觉带 --chapter 曾被 argparse 直接判死。现在：
    回声对得上活动章则放行，对不上明确报错；无 ack 在飞也明确报错。"""
    store = isolated_store(tmp_path)
    # 无 ack 在飞：先给出可教学的错误而不是 argparse unrecognized。
    assert main(["chapter", "ack-read", "--project", str(store.project), "--chapter", "1", "--quote", "任意"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_chapter"

    assembly(store)
    output = _assembly_output(store)
    output["plot_findings"] = []
    assert submit_output(store, output)["verdict"] == "accepted"
    chapter_next(store)  # await_ack

    assert main([
        "chapter", "ack-read", "--project", str(store.project),
        "--chapter", "9", "--quote", "任意",
    ]) == 1
    mismatch = json.loads(capsys.readouterr().out)["error"]
    assert mismatch["code"] == "invalid_chapter"
    assert mismatch["details"]["active_chapter"] == 1

    args = ["chapter", "ack-read", "--project", str(store.project), "--chapter", "1"]
    for quote in QUOTES:
        args += ["--quote", quote]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
