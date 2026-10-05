"""Run the actual review submission through the process driver and CLI fence."""
from __future__ import annotations

import sys
from pathlib import Path

from novel_ledger_core.control.autopilot import run_supervisor, validate_driver_config
from novel_ledger_core.control.pipeline import book_close_early, book_complete
from novel_ledger_core.infra.util import atomic_json, read_json
from tests.test_story_review import pass_pending, story_store, write_chapter


def completed_text(tmp_path):
    store = story_store(tmp_path, chapters=2)
    write_chapter(store)
    write_chapter(store)
    cfg = store.load_config()
    cfg["execution_mode"] = "inline"
    store.save_config(cfg)
    return store


def review_driver(tmp_path, *, result=True, session="unique", fresh=True):
    worker = tmp_path / "review-driver.py"
    scripts = str(Path(__file__).resolve().parents[1])
    worker.write_text(f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
from novel_ledger_core.control.cli import main
project=Path(os.environ['NOVEL_LEDGER_PROJECT'])
action=json.loads((project/'book/run/autopilot-current.action.json').read_text())
assert action['action']=='story_review'
view=json.loads(Path(action['review_pack_path']).read_text())
evidence=[{{'chapter':view['first'],'quote':'决定放弃奖金去救掌柜。'}},{{'chapter':view['through'],'quote':'也明白旧日承诺必须兑现。'}}]
checks=[{{'id':key,'status':'pass','reason':'人物选择的代价与卷末承诺已有正文支撑。','evidence':evidence}} for key in view['required_checks']]
output=Path(action['submit_output_path'])
output.write_text(json.dumps({{'review_id':view['review_id'],'input_hash':view['input_hash'],'checks':checks}},ensure_ascii=False))
code=main(['review','story-submit','--project',str(project),'--output',str(output)])
if code: sys.exit(code)
if {result!r}:
    receipt={{'status':'success','job_id':os.environ['NOVEL_LEDGER_JOB_ID'],'action':'story_review','context_origin':'empty','session_id':os.environ['NOVEL_LEDGER_JOB_ID'] if {session!r}=='unique' else 'reused-review'}}
    Path(os.environ['NOVEL_LEDGER_RESULT_FILE']).write_text(json.dumps(receipt))
''', encoding="utf-8")
    driver = {"kind": "command", "argv": [sys.executable, str(worker)]}
    if fresh:
        driver["session_policy"] = "fresh"
    path = tmp_path / "review-driver.json"
    atomic_json(path, {"schema": "novel-ledger.driver.v1", "driver": driver,
                      "limits": {"chapter_timeout_seconds": 10, "plan_timeout_seconds": 10,
                                 "no_progress_seconds": 10, "max_infra_retries": 0, "retry_backoff_seconds": [0]}})
    return validate_driver_config(path)


def test_inline_runner_dispatches_fresh_volume_and_book_reviews_then_seals_head(tmp_path):
    store = completed_text(tmp_path)
    config = review_driver(tmp_path)
    for scope in ("volume", "book"):
        result = run_supervisor(store, config, once=True)
        assert result["action"] == "run_job_complete"
        assert result["job"]["kind"] == "review"
        assert store.read_head()["last_story_review"]["review_id"].startswith(scope + ":")
    sessions = read_json(store.run_dir / "stage-sessions.json")
    assert len(sessions) == 2
    assert {row["action"] for row in sessions.values()} == {"story_review"}
    assert run_supervisor(store, config, once=True)["action"] == "run_complete"
    head = store.read_head()
    assert (head["status"], head["phase"], head["completion_kind"]) == ("completed", "complete", "normal")


def test_inline_review_also_requires_a_fresh_driver(tmp_path):
    store = completed_text(tmp_path)
    result = run_supervisor(store, review_driver(tmp_path, fresh=False), once=True)
    assert result["action"] == "run_paused"
    assert result["error"]["code"] == "stage_driver_not_isolated"
    assert not store.autopilot_active_job_path.exists()


def test_review_receipt_missing_keeps_fence_and_can_recover_without_skipping(tmp_path):
    store = completed_text(tmp_path)
    config = review_driver(tmp_path, result=False)
    assert run_supervisor(store, config, once=True)["action"] == "run_paused"
    assert store.autopilot_active_job_path.exists()
    assert run_supervisor(store, config, once=True)["reason"] == "stage_session_unverified"
    job = read_json(store.autopilot_state_path)["current_job"]
    atomic_json(store.run_dir / "autopilot-current.result.json", {
        "job_id": job["job_id"], "action": "story_review", "session_id": "recovered-volume-review",
        "context_origin": "empty", "status": "success"})
    assert run_supervisor(store, review_driver(tmp_path), once=True)["action"] == "run_job_complete"
    assert store.read_head()["last_story_review"]["review_id"].startswith("book:")
    assert len(read_json(store.run_dir / "stage-sessions.json")) == 2


def test_volume_session_cannot_be_reused_for_book_review(tmp_path):
    store = completed_text(tmp_path)
    config = review_driver(tmp_path, session="reuse")
    assert run_supervisor(store, config, once=True)["action"] == "run_job_complete"
    failed = run_supervisor(store, config, once=True)
    assert failed["action"] == "run_paused"
    assert failed["detail"]["error"]["code"] == "stage_session_reused"
    assert store.autopilot_active_job_path.exists()
    assert store.read_head()["status"] == "active"


def test_author_early_closure_stops_runner_without_normal_completion_audit(tmp_path):
    store = story_store(tmp_path)
    book_close_early(store, actor="作者", reason="明确提前封笔", author_confirmed=True)
    result = run_supervisor(store, review_driver(tmp_path), once=True)
    assert result["action"] == "run_stopped"
    assert result["reason"] == "author_early_close"
    assert result["normal_completion"] is False
    assert store.read_head()["completion_kind"] == "early_close"
    assert "completion_audit" not in read_json(store.autopilot_state_path)


def test_completed_book_is_revalidated_after_editorial_sources_change(tmp_path):
    store = completed_text(tmp_path)
    config = review_driver(tmp_path)
    run_supervisor(store, config, once=True)
    run_supervisor(store, config, once=True)
    assert run_supervisor(store, config, once=True)["action"] == "run_complete"
    store.outline_path.write_text("## 主线\n作者已修改终局条件，需要复核。\n", encoding="utf-8")
    result = run_supervisor(store, config, once=True)
    assert result["action"] == "run_paused"
    assert result["error"]["code"] == "story_review_required"


def test_explicit_author_word_override_survives_completed_book_revalidation(tmp_path):
    store = completed_text(tmp_path)
    cfg = store.load_config()
    cfg["book_words"] = 1000
    store.save_config(cfg)
    pass_pending(store, completing=True)
    pass_pending(store, completing=True)
    result = book_complete(store, actor="作者", reason="明确缩短本书字数目标", override_target=True)
    assert result["target_overridden"] is True
    assert run_supervisor(store, review_driver(tmp_path), once=True)["action"] == "run_complete"
    assert store.read_head()["completion_target_override"] == 1000
