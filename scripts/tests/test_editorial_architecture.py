from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from novel_ledger_core.control.autopilot import run_supervisor, validate_driver_config, _checkpoint_report
from novel_ledger_core.control.autopilot import _record_result_usage, build_worker_prompt
from novel_ledger_core.control.stage_jobs import record_session_receipt
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    chapter_next, stage_draft_submit, stage_polish_submit, submit_output,
    ack_read, patch_prose, ack_patched_chapter, check_submit_output,
    book_complete,
)
from novel_ledger_core.content.reviews import list_findings, resolve_finding
from novel_ledger_core.infra.util import LedgerError, read_json, atomic_json, chinese_word_count
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


def test_stage_fence_refuses_next_and_cross_role_submission(tmp_path, monkeypatch, capsys):
    store = isolated_store(tmp_path)
    action = chapter_next(store)
    assert action["execution"]["context_isolation"] == "fresh_session"
    assert action["worker"]["context_origin"] == "empty"
    assert action["worker"]["protocol"] == "stage"
    atomic_json(store.autopilot_active_job_path, {"job_id": "draft-job", "kind": "stage", "target": 1, "initial_action": "draft"})
    monkeypatch.setenv("NOVEL_LEDGER_JOB_ID", "draft-job")
    for command in ("next", "polish-submit"):
        assert main(["chapter", command, "--project", str(store.project)]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "stage_capability_violation"
    Path(action["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    assert main(["chapter", "draft-submit", "--project", str(store.project)]) == 0
    capsys.readouterr()
    assert main(["chapter", "draft-submit", "--project", str(store.project)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "stage_capability_violation"


def driver(tmp_path, *, session="unique", result=True):
    scripts_dir = Path(__file__).resolve().parents[1]
    worker = tmp_path / "stage-driver.py"
    worker.write_text(f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(scripts_dir)!r})
from novel_ledger_core.control.cli import main
project=Path(os.environ['NOVEL_LEDGER_PROJECT'])
action=json.loads((project/'book/run/autopilot-current.action.json').read_text())
args=['--terse', 'chapter']
prose={PROSE!r}
stage=action['action']
if stage=='draft':
    Path(action['draft_output_path']).write_text(prose)
    args+=['draft-submit']
elif stage=='polish':
    Path(action['polished_output_path']).write_text(prose)
    args+=['polish-submit']
elif stage=='assemble':
    pack=json.loads(Path(action['assemble_pack_path']).read_text())
    output={{'l1_summary':'主角拒收改期，掌柜仍扣着凭据。','state_delta':{{'moves':[{{'who':'主角','to':'市集'}}],'facts':[],'debts':[],'hooks':[],'relations':[],'named':['主角','掌柜'],'new_names':[],'deaths':[]}},'memory':{{}},'pack_hash':pack['pack_hash'],'beats_hit':['b1','b2'],'plot_findings':[]}}
    path=Path(action['submit_output_path']);path.write_text(json.dumps(output,ensure_ascii=False))
    args+=['submit','--output',str(path)]
else:
    args+=['ack-read']
    for quote in {QUOTES!r}: args+=['--quote',quote]
code=main(args+['--project',str(project)])
if code: sys.exit(code)
if {result!r}:
    receipt={{'job_id':os.environ['NOVEL_LEDGER_JOB_ID'],'action':stage,'session_id':('test-process-'+str(os.getpid())) if {session!r}=='unique' else 'reused','context_origin':'empty','status':'success'}}
    Path(os.environ['NOVEL_LEDGER_RESULT_FILE']).write_text(json.dumps(receipt))
''', encoding="utf-8")
    config_path = tmp_path / "driver-config.json"
    atomic_json(config_path, {"schema":"novel-ledger.driver.v1", "driver":{"kind":"command", "session_policy":"fresh", "argv":[sys.executable,str(worker)]}, "limits":{"chapter_timeout_seconds":10,"plan_timeout_seconds":10,"no_progress_seconds":10,"max_infra_retries":0,"retry_backoff_seconds":[0]}})
    return validate_driver_config(config_path)


def test_runner_runs_four_distinct_stage_sessions_and_only_one_chapter(tmp_path):
    store = isolated_store(tmp_path)
    outcome = run_supervisor(store, driver(tmp_path), once=False, max_chapters=1)
    assert outcome["action"] == "run_stopped"
    sessions = read_json(store.run_dir / "stage-sessions.json")
    assert len(sessions) == 4
    assert {r["action"] for r in sessions.values()} == {"draft","polish","assemble","ack"}
    assert store.read_head()["last_acked_ch"] == 1
    assert read_json(store.autopilot_state_path)["chapters_completed"] == 1
    assert not store.autopilot_active_job_path.exists()


def test_runner_rejects_reused_context_and_missing_receipt_cannot_be_skipped(tmp_path):
    store = isolated_store(tmp_path)
    config = driver(tmp_path, session="reuse")
    assert run_supervisor(store, config, once=True)["action"] == "run_job_complete"
    failed = run_supervisor(store, config, once=True)
    assert failed["action"] == "run_paused"
    assert failed["detail"]["error"]["code"] == "stage_session_reused"
    resumed = run_supervisor(store, config, once=True)
    assert resumed["action"] == "run_paused"
    assert resumed["reason"] == "stage_session_reused"


def test_completed_stage_without_session_receipt_remains_fenced_until_recovery(tmp_path):
    store = isolated_store(tmp_path)
    config = driver(tmp_path, result=False)
    assert run_supervisor(store, config, once=True)["action"] == "run_paused"
    assert store.read_head()["phase"] == "await_polish"
    assert store.autopilot_active_job_path.exists()
    assert run_supervisor(store, config, once=True)["reason"] == "stage_session_unverified"
    state = read_json(store.autopilot_state_path)
    job = state["current_job"]
    atomic_json(store.run_dir / "autopilot-current.result.json", {"job_id":job["job_id"],"action":"draft","session_id":"recovered-host-session","context_origin":"empty"})
    assert run_supervisor(store, driver(tmp_path), once=True)["action"] == "run_job_complete"
    assert store.read_head()["phase"] == "await_assembly"


def test_usage_records_are_request_deltas_and_replay_does_not_double_count(tmp_path):
    store = isolated_store(tmp_path)
    chapter_next(store)
    job = {"job_id": "job-x", "kind": "stage", "initial_action": "draft", "target": 1}
    receipt = {"session_id": "session-x", "usage_records": [
        {"request_id":"job-x:1", "usage":{"uncached_input_tokens":10,"output_tokens":2}},
        {"request_id":"job-x:2", "usage":{"uncached_input_tokens":20,"output_tokens":3}},
    ]}
    _record_result_usage(store, job=job, worker_result=receipt)
    first = store.usage_path.read_bytes()
    _record_result_usage(store, job=job, worker_result=receipt)
    assert store.usage_path.read_bytes() == first
    assert len(first.splitlines()) == 2
    with pytest.raises(LedgerError) as exc:
        _record_result_usage(store, job=job, worker_result={"usage":{"uncached_input_tokens":30}})
    assert exc.value.code == "stage_usage_aggregate_forbidden"


def test_plan_job_is_also_empty_context_and_must_report_host_session(tmp_path):
    store = isolated_store(tmp_path)
    prompt = build_worker_prompt(project=store.project, job_id="plan-x", kind="plan", chapter=1)
    assert "禁止运行 chapter next" in prompt
    assert "plan_worker_brief.path" in prompt
    job = {"job_id":"plan-x", "kind":"plan", "isolation_required":True, "initial_action":"extend_plan", "target":1}
    with pytest.raises(LedgerError):
        record_session_receipt(store, job, {})
    record_session_receipt(store, job, {"job_id":"plan-x","action":"extend_plan","session_id":"plan-session","context_origin":"empty"})
    assert read_json(store.run_dir / "stage-sessions.json")["plan-session"]["action"] == "extend_plan"


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


def test_assembly_cannot_submit_an_undeclared_output_file(tmp_path, monkeypatch, capsys):
    store = isolated_store(tmp_path)
    action = assembly(store)
    atomic_json(store.autopilot_active_job_path, {"job_id":"assembly-job","kind":"stage","target":1,"initial_action":"assemble","output_path":action["submit_output_path"]})
    monkeypatch.setenv("NOVEL_LEDGER_JOB_ID", "assembly-job")
    other = store.staging_dir / "other.json"
    atomic_json(other, {**_assembly_output(store), "plot_findings": []})
    assert main(["chapter","submit","--project",str(store.project),"--output",str(other)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "stage_capability_violation"
    assert store.read_head()["phase"] == "await_assembly"


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
