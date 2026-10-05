from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from novel_ledger_core.control.autopilot import (
    AUTOPILOT_SCHEMA,
    DRIVER_SCHEMA,
    SupervisorLease,
    build_worker_prompt,
    request_pause,
    resume_run,
    run_supervisor,
    validate_driver_config,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import chapter_next, record_usage
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json


def _store(tmp_path: Path, *, chapters: int = 45, low_water: int = 0) -> BookStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = {
        "protagonist": "主角",
        "chapters": [
            {
                "chapter": number,
                "location": "试炼场",
                "present": ["主角"],
                "beats": [
                    {
                        "id": "b1",
                        "required": True,
                        "text": f"主角完成第{number}步试炼",
                        "must": "试炼",
                    }
                ],
            }
            for number in range(1, chapters + 1)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "book-project"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = low_water
    cfg["style_check"] = "off"
    store.save_config(cfg)
    return store


def _fake_worker(tmp_path: Path, *, sleep: int = 0, usage: bool = False) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / f"fake-worker-{sleep}-{int(usage)}.py"
    body = """
import json, os, pathlib, sys, time
project = pathlib.Path(sys.argv[1])
result = pathlib.Path(sys.argv[2])
time.sleep(SLEEP_SECONDS)
sys.path.insert(0, SKILL_SCRIPTS)
from novel_ledger_core.infra.store import BookStore
store = BookStore(project)
head = store.read_head()
chapter = int(head.get("chapter") or 0)
head.update({"phase":"idle", "status":"active", "last_committed_ch":chapter, "last_acked_ch":chapter})
store.write_head(head)
with (project / "jobs.log").open("a", encoding="utf-8") as handle:
    handle.write(os.environ["NOVEL_LEDGER_JOB_ID"] + "\\n")
payload = {"status":"success", "session_id":os.environ["NOVEL_LEDGER_JOB_ID"]}
if WRITE_USAGE:
    payload["usage"] = {"uncached_input_tokens":100, "cache_read_input_tokens":25, "cache_write_input_tokens":0, "output_tokens":50}
result.write_text(json.dumps(payload), encoding="utf-8")
""".replace("SLEEP_SECONDS", str(sleep)).replace("WRITE_USAGE", "True" if usage else "False").replace("SKILL_SCRIPTS", repr(str(Path(__file__).resolve().parents[1])))
    script.write_text(body, encoding="utf-8")
    return script


def _config(
    tmp_path: Path,
    script: Path,
    *,
    retries: int = 0,
    timeout: int = 5,
) -> dict:
    argv = [sys.executable, str(script), "{project}", "{result_file}"]
    payload: dict = {
        "schema": DRIVER_SCHEMA,
        "driver": {
            "kind": "command",
            "argv": argv,
            "cwd": "{project}",
        },
        "limits": {
            "chapter_timeout_seconds": timeout,
            "plan_timeout_seconds": timeout,
            "no_progress_seconds": timeout,
            "max_infra_retries": retries,
            "retry_backoff_seconds": [0],
        },
    }
    path = tmp_path / "driver.json"
    atomic_json(path, payload)
    return validate_driver_config(path)


def test_driver_config_and_worker_prompt_are_provider_neutral(tmp_path: Path):
    worker = _fake_worker(tmp_path)
    config = _config(tmp_path, worker)
    assert config["schema"] == DRIVER_SCHEMA
    prompt = build_worker_prompt(project=tmp_path, job_id="job-1", kind="chapter", chapter=7)
    assert "第7章" in prompt
    assert "禁止创建、派发或 fork 任何物理子 agent" in prompt
    assert "requires_new_session=true" in prompt
    assert "NOVEL_LEDGER_JOB_ID=job-1" in prompt
    assert "/goal" not in prompt


def test_invalid_driver_placeholder_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.json"
    atomic_json(
        path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["model", "{secret}"]},
            "limits": {},
        },
    )
    with pytest.raises(LedgerError, match="unknown argv placeholder"):
        validate_driver_config(path)


def test_validate_config_reports_unlaunchable_argv_before_dispatch(tmp_path: Path):
    """接线期就要报出"这条 argv 在本机起不来"，别等到运行期只看到 dispatch_failed。

    argv 用裸 `dsh`（Windows 上是 .CMD 垫片）时 subprocess 不走
    shell、裸名只匹配 .exe，运行期直接 FileNotFoundError；而 validate-config 当时只校验
    schema，接线看起来是通过的，排查被带到 PATH 上。
    """
    from novel_ledger_core.control.autopilot import executable_resolution_warnings, validate_config_response

    # 1) 裸名字解析不到 → not_found（不阻断，schema 仍然 ok）
    unresolved = tmp_path / "unresolved.json"
    atomic_json(
        unresolved,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["definitely-not-on-path-9f3a", "{prompt_file}"]},
            "limits": {},
        },
    )
    payload = validate_config_response(unresolved)
    assert payload["ok"] is True
    codes = [item["code"] for item in payload["executable_warnings"]]
    assert codes == ["driver_executable_not_found", "external_driver_model_unmetered"]
    assert "dispatch_failed" in payload["hint"]

    # 2) 真实存在的解释器完整路径 → 没有告警
    healthy = tmp_path / "healthy.json"
    atomic_json(
        healthy,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": [sys.executable, "{prompt_file}"]},
            "limits": {},
        },
    )
    # command driver 恒带外部计费告警（模型随外部进程自身账户走）
    healthy_codes = [
        item["code"]
        for item in (validate_config_response(healthy).get("executable_warnings") or [])
    ]
    assert healthy_codes == ["external_driver_model_unmetered"]

    # 3) 占位符还没展开的 argv[0] 交给运行期，不在校验期乱报
    assert executable_resolution_warnings({"kind": "command", "argv": ["{job_id}", "{prompt_file}"]}) == []

    # 4) 只解析到 .cmd/.bat 垫片时点名提示（Windows 语义；其他平台用假 resolved 直接测分支）
    if os.name == "nt":
        import shutil as _shutil

        cmd = _shutil.which("cmd") or _shutil.which("cmd.exe")
        if cmd and Path(cmd).suffix.lower() in (".cmd", ".bat", ".exe") and Path(cmd).suffix.lower() != ".exe":
            warnings = executable_resolution_warnings({"kind": "command", "argv": ["cmd"]})
            assert warnings and warnings[0]["code"] == "driver_executable_is_shell_shim"


def test_command_driver_rejects_shell_mode(tmp_path: Path):
    path = tmp_path / "shell.json"
    atomic_json(
        path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["model"], "shell": True},
            "limits": {},
        },
    )
    with pytest.raises(LedgerError, match="shell=true"):
        validate_driver_config(path)


def test_forty_chapters_use_forty_fresh_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # This worker only advances HEAD.  Checkpoint artifact validation is covered by
    # test_autopilot_checkpoints; keep this test focused on dispatch cadence.
    from novel_ledger_core.control import autopilot

    monkeypatch.setattr(
        autopilot,
        "_checkpoint_report",
        lambda store, *, chapter, previous, kinds: {
            "chapter": chapter, "kinds": list(kinds), "review_required": False, "blockers": [],
        },
    )
    store = _store(tmp_path)
    config = _config(tmp_path, _fake_worker(tmp_path))
    result = run_supervisor(store, config, once=False, max_chapters=40)
    assert result["reason"] == "max_chapters"
    assert store.read_head()["last_acked_ch"] == 40
    ids = (store.project / "jobs.log").read_text(encoding="utf-8").splitlines()
    assert len(ids) == 40
    assert len(set(ids)) == 40
    assert not store.autopilot_active_job_path.exists()
    events = [json.loads(line) for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()]
    assert len([event for event in events if event["event"] == "job_started"]) == 40
    assert [event["chapter"] for event in events if event["event"] == "quality_checkpoint"] == [10, 20, 30, 40]


def test_exact_usage_is_recorded_after_ack_and_unknown_is_not_zero(tmp_path: Path):
    reported = _store(tmp_path / "reported", chapters=2)
    config = _config(tmp_path / "reported", _fake_worker(tmp_path / "reported", usage=True))
    result = run_supervisor(reported, config, once=True)
    assert result["telemetry"] == "reported"
    usage = [json.loads(line) for line in reported.usage_path.read_text(encoding="utf-8").splitlines()]
    assert usage[0]["chapter"] == 1
    assert usage[0]["effective_input_tokens"] == 125

    unknown = _store(tmp_path / "unknown", chapters=2)
    config = _config(tmp_path / "unknown", _fake_worker(tmp_path / "unknown", usage=False))
    result = run_supervisor(unknown, config, once=True)
    assert result["telemetry"] == "unknown"
    assert not unknown.usage_path.exists()


def test_command_timeout_terminates_and_pauses(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    config = _config(tmp_path, _fake_worker(tmp_path, sleep=5), timeout=1)
    result = run_supervisor(store, config, once=True)
    assert result["action"] == "run_paused"
    assert result["reason"] in ("timeout", "no_progress")
    state = read_json(store.autopilot_state_path)
    assert state["status"] == "paused"
    assert not store.autopilot_active_job_path.exists()
    events = store.autopilot_events_path.read_text(encoding="utf-8")
    assert '"terminated":true' in events


def test_command_no_progress_retries_three_times_with_fresh_jobs(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    worker = tmp_path / "no-progress.py"
    worker.write_text(
        "import os, pathlib, sys\n"
        "with (pathlib.Path(sys.argv[1]) / 'attempts.log').open('a') as f: "
        "f.write(os.environ['NOVEL_LEDGER_JOB_ID'] + '\\n')\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, worker, retries=3)
    result = run_supervisor(store, config, once=True)
    assert result["reason"] == "no_progress"
    ids = (store.project / "attempts.log").read_text(encoding="utf-8").splitlines()
    assert len(ids) == 4
    assert len(set(ids)) == 4
    assert not store.autopilot_active_job_path.exists()
    events = [json.loads(line) for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()]
    waits = [event for event in events if event["event"] == "retry_wait"]
    assert [event["seconds"] for event in waits] == [0, 0, 0]


def test_plan_low_water_finishes_before_chapter_dispatch(tmp_path: Path):
    store = _store(tmp_path, chapters=2, low_water=1)
    # 「首次 next 直接 draft」：新书 3 章种子不触发低水位提示（见
    # test_choice_hatch 的 default-config 用例），所以本用例预置第 1 章已提交，
    # 制造书中部低水位——这才是该提示的设计场景。
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)
    worker = tmp_path / "plan-then-chapter.py"
    worker.write_text(
        "import json, os, pathlib, sys\n"
        "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
        "from novel_ledger_core.infra.store import BookStore\n"
        "project = pathlib.Path(sys.argv[1])\n"
        "store = BookStore(project)\n"
        "result = pathlib.Path(sys.argv[2])\n"
        "prompt = pathlib.Path(os.environ['NOVEL_LEDGER_PROMPT_FILE']).read_text()\n"
        "kind = 'plan' if '只完成当前 extend_plan' in prompt else 'chapter'\n"
        "if kind == 'plan':\n"
        "    plan = store.load_plan()\n"
        "    last = plan['chapters'][-1]\n"
        "    added = dict(last, chapter=int(last['chapter']) + 1)\n"
        "    plan['chapters'].append(added)\n"
        "    store.save_plan(plan)\n"
        "else:\n"
        "    head = store.read_head()\n"
        "    chapter = int(head.get('chapter') or 0)\n"
        "    head.update({'phase':'idle','status':'active','last_committed_ch':chapter,'last_acked_ch':chapter})\n"
        "    store.write_head(head)\n"
        "with (project / 'order.log').open('a') as f:\n"
        "    f.write(kind + ':' + os.environ['NOVEL_LEDGER_JOB_ID'] + '\\n')\n"
        "result.write_text(json.dumps({'status':'success'}))\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, worker)
    result = run_supervisor(store, config, once=False, max_chapters=1)
    assert result["reason"] == "max_chapters"
    order = (store.project / "order.log").read_text(encoding="utf-8").splitlines()
    assert [entry.split(":", 1)[0] for entry in order] == ["plan", "chapter"]
    assert len({entry.split(":", 1)[1] for entry in order}) == 2


def test_agentapi_timeout_never_dispatches_a_retry(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    # 假的可执行文件必须能在当前平台被真正 dispatch：Windows 不能直接执行无扩展名的
    # `#!/bin/sh` 脚本（WinError 193），那会让 dispatch 失败并触发重试，测试就测不到
    # "已派发但无法取消"这条路径。按平台选载体，两边都返回 0 并回显会话号。
    if os.name == "nt":
        fake = tmp_path / "agentapi.cmd"
        fake.write_text("@echo off\r\necho conversation-1\r\n", encoding="utf-8")
    else:
        fake = tmp_path / "agentapi"
        fake.write_text("#!/bin/sh\necho conversation-1\n", encoding="utf-8")
        fake.chmod(0o755)
    config_path = tmp_path / "agentapi.json"
    atomic_json(
        config_path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "antigravity-agentapi", "executable": str(fake)},
            "limits": {
                "chapter_timeout_seconds": 1,
                "plan_timeout_seconds": 1,
                "no_progress_seconds": 1,
                "max_infra_retries": 3,
                "retry_backoff_seconds": [0],
            },
        },
    )
    result = run_supervisor(store, validate_driver_config(config_path), once=True)
    assert result["action"] == "run_paused"
    events = [json.loads(line) for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()]
    assert len([event for event in events if event["event"] == "job_started"]) == 1
    assert any(event.get("uncancellable") is True for event in events if event["event"] == "paused")
    assert store.autopilot_active_job_path.exists()


def test_active_job_fence_rejects_other_writers_but_not_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    store = _store(tmp_path, chapters=2)
    atomic_json(
        store.autopilot_active_job_path,
        {
            "schema": AUTOPILOT_SCHEMA,
            "job_id": "job-owner",
            "kind": "chapter",
            "target": 1,
            "driver_kind": "command",
        },
    )
    monkeypatch.delenv("NOVEL_LEDGER_JOB_ID", raising=False)
    assert main(["chapter", "next", "--project", str(store.project)]) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"]["code"] == "autopilot_fence"

    assert main(["run", "status", "--project", str(store.project)]) == 0
    visible = json.loads(capsys.readouterr().out)
    assert visible["active_job"]["job_id"] == "job-owner"

    monkeypatch.setenv("NOVEL_LEDGER_JOB_ID", "job-owner")
    assert main(["chapter", "next", "--project", str(store.project)]) == 0
    allowed = json.loads(capsys.readouterr().out)
    assert allowed["action"] == "draft"


def test_blocked_and_complete_do_not_dispatch_workers(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    head = store.read_head()
    head.update({"phase": "blocked", "blocked": {"reason": "test"}})
    store.write_head(head)
    config = _config(tmp_path, _fake_worker(tmp_path))
    blocked = run_supervisor(store, config, once=True)
    assert blocked["reason"] == "blocked"
    assert not (store.project / "jobs.log").exists()

    resume_run(store)
    head = store.read_head()
    head.update({"phase": "complete", "status": "completed"})
    store.write_head(head)
    complete = run_supervisor(store, config, once=True)
    assert complete["reason"] == "completion_audit_failed"
    assert "empty_book" in {item["code"] for item in complete["blockers"]}
    assert not (store.project / "jobs.log").exists()


def test_usage_guard_and_event_spine_damage_pause_without_dispatch(tmp_path: Path):
    guarded = _store(tmp_path / "guarded", chapters=2)
    cfg = guarded.load_config()
    cfg["usage_budget"] = {"warn_input_per_chapter": 1, "stop_input_per_chapter": 1}
    guarded.save_config(cfg)
    record_usage(
        guarded,
        stage="host",
        usage={"uncached_input_tokens": 2},
        chapter=1,
        request_id="guard-before-start",
    )
    guarded_config = _config(tmp_path / "guarded", _fake_worker(tmp_path / "guarded"))
    stopped = run_supervisor(guarded, guarded_config, once=True)
    assert stopped["reason"] == "usage_guard"
    assert not (guarded.project / "jobs.log").exists()

    damaged = _store(tmp_path / "damaged", chapters=2)
    atomic_json(damaged.hatch_manifest_path, {})
    damaged_config = _config(tmp_path / "damaged", _fake_worker(tmp_path / "damaged"))
    stopped = run_supervisor(damaged, damaged_config, once=True)
    assert stopped["reason"] == "contract_violation"
    assert stopped["error"]["code"] == "event_spine_contract_violation"
    assert not (damaged.project / "jobs.log").exists()

def test_pause_and_resume_are_out_of_band(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    paused = request_pause(store, "operator check")
    assert paused["paused"] is True
    assert read_json(store.autopilot_pause_path)["reason"] == "operator check"
    resumed = resume_run(store)
    assert resumed["resumed"] is True
    assert not store.autopilot_pause_path.exists()


def test_crash_recovery_resumes_half_chapter_in_a_fresh_job(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    with store.exclusive_lock():
        action = chapter_next(store)
    assert action["action"] == "draft"
    old_job = "crashed-job"
    atomic_json(
        store.autopilot_state_path,
        {
            "schema": AUTOPILOT_SCHEMA,
            "status": "running",
            "current_job": {
                "job_id": old_job,
                "kind": "chapter",
                "target": 1,
                "initial_action": "draft",
                "before": {"last_acked_ch": 0},
                "attempt": 0,
                "driver_kind": "command",
                "driver_handle": {"pid": 99999999},
            },
        },
    )
    atomic_json(
        store.autopilot_active_job_path,
        {
            "schema": AUTOPILOT_SCHEMA,
            "job_id": old_job,
            "kind": "chapter",
            "target": 1,
            "driver_kind": "command",
        },
    )
    config = _config(tmp_path, _fake_worker(tmp_path))
    result = run_supervisor(store, config, once=True)
    assert result["action"] == "run_job_complete"
    ids = (store.project / "jobs.log").read_text(encoding="utf-8").splitlines()
    assert len(ids) == 1
    assert ids[0] != old_job
    assert store.read_head()["last_acked_ch"] == 1


def test_live_supervisor_lease_rejects_second_owner(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    with SupervisorLease(store):
        with pytest.raises(LedgerError, match="another unattended supervisor"):
            SupervisorLease(store).__enter__()


def test_dead_stale_supervisor_lease_is_reclaimed(tmp_path: Path):
    store = _store(tmp_path, chapters=2)
    store.autopilot_lease_dir.mkdir()
    atomic_json(
        store.autopilot_lease_dir / "owner.json",
        {
            "schema": AUTOPILOT_SCHEMA,
            "run_id": "dead",
            "pid": 99999999,
            "started_at": 1,
            "heartbeat_at": 1,
        },
    )
    with SupervisorLease(store) as lease:
        assert read_json(lease.owner_path)["run_id"] == lease.run_id
    assert not store.autopilot_lease_dir.exists()


def test_worker_protocol_version_is_single_sourced(tmp_path: Path):
    """supervisor 派发 prompt 与 `chapter next` 的 worker 简报共用同一协议版本常量。

    版本行是"宿主装了新旧两个 skill 实例混跑"的唯一可见信号（纪律见
    references/unattended.md「协议版本纪律」）：两侧各写一份字面量会静默分叉，
    所以必须同源，且此处钉住两路输出都真的携带它。
    """
    from novel_ledger_core.control.pipeline import WORKER_PROMPT_PROTOCOL, _worker_brief

    prompt = build_worker_prompt(project=tmp_path, job_id="job-1", kind="chapter", chapter=7)
    assert f"协议版本：{WORKER_PROMPT_PROTOCOL}" in prompt

    store = _store(tmp_path, chapters=2)
    nxt = chapter_next(store)
    chapter_brief = _worker_brief(store, nxt)
    assert chapter_brief["worker_protocol"] == WORKER_PROMPT_PROTOCOL
    plan_brief = _worker_brief(store, {"action": "extend_plan"})
    assert plan_brief["worker_protocol"] == WORKER_PROMPT_PROTOCOL


# ── 无进展窗口按可观察工件计（2026-09-29 双书实测重校准）────────────────────


def test_no_progress_default_recalibrated_for_chapter_latency() -> None:
    """单章实测涨到 ~15 分钟后,900s 会在草稿写完的同一分钟杀 worker。"""
    from novel_ledger_core.control.autopilot import (
        DEFAULT_LIMITS,
        NO_PROGRESS_FLOOR_SECONDS,
    )

    assert DEFAULT_LIMITS["no_progress_seconds"] == 1800
    assert NO_PROGRESS_FLOOR_SECONDS == 1800


def test_validate_config_warns_on_stale_no_progress_window(tmp_path: Path) -> None:
    """旧现场配置(900)在校验期被点名,而不是运行期反复杀 worker。"""
    from novel_ledger_core.control.autopilot import validate_config_response

    path = tmp_path / "stale.json"
    atomic_json(
        path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["model-cli", "{prompt_file}"]},
            "limits": {"no_progress_seconds": 900},
        },
    )
    resp = validate_config_response(path)
    warnings = resp.get("executable_warnings") or []
    item = next((w for w in warnings if w.get("code") == "plan_job_no_progress_tight"), None)
    assert item is not None
    assert item["recommended_floor_seconds"] == 1800


def test_snapshot_counts_staging_artifacts_as_progress(tmp_path: Path) -> None:
    """staging 草稿落盘=进度:写作中的 worker 不是挂死的 worker。"""
    from novel_ledger_core.control.autopilot import _snapshot

    store = _store(tmp_path)
    before = _snapshot(store)
    store.staging_dir.mkdir(parents=True, exist_ok=True)
    (store.staging_dir / "draft-0001.txt").write_text("第一段落。", encoding="utf-8")
    after = _snapshot(store)
    assert after != before
    assert any(name == "draft-0001.txt" for name, _size, _mtime in after["staging"])


def test_model_placeholder_is_retired(tmp_path: Path) -> None:
    """外部 CLI 的模型投喂已整体移除：argv 用 {model} 直接按未知占位符拒收。"""
    path = tmp_path / "driver-model.json"
    atomic_json(
        path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["model-cli", "{model}", "{prompt_file}"]},
            "limits": {},
        },
    )
    with pytest.raises(LedgerError, match="unknown argv placeholder"):
        validate_driver_config(path)


def test_command_driver_warns_external_model_unmetered(tmp_path: Path) -> None:
    """command driver 的模型与计费由外部进程自身决定——校验期点名,引导子 agent 形态。"""
    from novel_ledger_core.control.autopilot import validate_config_response

    path = tmp_path / "driver.json"
    atomic_json(
        path,
        {
            "schema": DRIVER_SCHEMA,
            "driver": {"kind": "command", "argv": ["model-cli", "{prompt_file}"]},
            "limits": {},
        },
    )
    resp = validate_config_response(path)
    codes = {str(w.get("code")) for w in (resp.get("executable_warnings") or [])}
    assert "external_driver_model_unmetered" in codes


def test_run_handoff_writes_machine_section(tmp_path: Path) -> None:
    """交接卡是恢复会话的零考古入口：HEAD/下一动作/临期钩子/开放 findings 全进一张卡。"""
    from novel_ledger_core.control.autopilot import handoff_report
    from novel_ledger_core.ledger.ledger import commit_event

    store = _store(tmp_path, chapters=6)
    commit_event(
        store,
        chapter=1,
        state_delta={
            "named": ["主角"],
            "facts": [],
            "hooks": [
                {"id": "h-soon", "text": "临期约定", "due": 2, "status": "open"},
                {"id": "h-far", "text": "远期约定", "due": 90, "status": "open"},
            ],
        },
    )
    head_before = store.read_head()
    result = handoff_report(store)
    assert result["ok"] is True and result["action"] == "run_handoff"
    assert result["due_hook_count"] == 1
    text = (store.run_dir / "handoff.md").read_text(encoding="utf-8")
    assert "下一动作" in text
    assert "临期约定" in text, "due ≤ chapter+3 的 open 钩子必须进卡"
    assert "远期约定" not in text, "远期钩子不进卡，防止卡片膨胀"
    assert "开放 findings" in text
    assert "unattended-log.md 尚未创建" in text
    # 交接只读：HEAD 相位与章号不得被推进。
    assert store.read_head() == head_before


def test_run_relay_prompt_bans_self_renewal_and_is_state_derived(tmp_path: Path) -> None:
    """接力 prompt 是运行态的确定性产物，且把平台铁律写死：定时任务会话永不自建自动化。

    实测断链：手写接力模板让被拉起的会话批末 CronCreate 下一棒，被平台硬拒
    （"Cannot create a scheduled task inside a session that already belongs to a
    scheduled task"），链条断裂；此前还有 CronList 连发 50+ 次的空转。
    正确结构只有一个 recurring 定时器，每火一批，下一棒=下一次触发。
    """
    from novel_ledger_core.control.autopilot import relay_prompt

    store = _store(tmp_path, chapters=6)
    # 接力形态是选择加入：批界默认已关（作者裁决=主线程循环连写），本测试显式开回
    # 验证 relay prompt 如实反映 config.host_batch_chapters。
    cfg = store.load_config()
    cfg["host_batch_chapters"] = 5
    store.save_config(cfg)
    resp = relay_prompt(store, every_hours=4)
    assert resp["ok"] is True and resp["action"] == "run_relay_prompt"
    assert resp["schedule"]["kind"] == "recurring"
    assert resp["schedule"]["interval"] == 4
    assert resp["creator"].startswith("author session in the BOOK's workspace")

    text = (store.run_dir / "relay-prompt.md").read_text(encoding="utf-8")
    assert "禁止调用 CronCreate" in text, "被拉起会话自建自动化会被平台拒绝，必须写死禁令"
    assert "禁止调用 CronList" in text
    assert str(store.project) in text
    assert "run handoff --host-session-start" in text
    assert str(store.run_dir / "handoff.md") in text
    assert "chapter usage-record" in text and "chapter usage-void" in text
    assert "批大小 5 章" in text, "批大小取 config.host_batch_chapters 默认值"
    # 事件留痕：relay prompt 的生成可审计（autopilot 事件流，非账本链）。
    import json as _json

    lines = [
        _json.loads(line)
        for line in store.autopilot_events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "run_relay_prompt" for e in lines), "run_relay_prompt 事件必须留痕"
