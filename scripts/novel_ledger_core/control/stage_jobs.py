"""Stage capabilities and fresh-session receipts, shared by drivers and CLI fences.

The host owns creation of an empty model conversation. Session receipts detect
reuse; they are host attestations, not a filesystem sandbox or proof of reading.
"""
from __future__ import annotations

from typing import Any
from pathlib import Path

from ..infra.store import BookStore
from ..infra.util import LedgerError, atomic_json, read_json, now_ts

STAGE_PHASES = {
    "draft": {"await_polish", "await_assembly"},
    "polish": {"await_assembly"},
    "assemble": {"submitted", "await_draft"},
    "ack": {"idle", "await_draft"},
}
STAGE_COMMANDS = {
    "draft": {("chapter", "draft-submit"), ("chapter", "usage-record")},
    "polish": {("chapter", "polish-submit"), ("chapter", "patch"), ("chapter", "usage-record")},
    "assemble": {("chapter", "submit"), ("chapter", "usage-record")},
    "ack": {("chapter", "ack-read"), ("chapter", "usage-record")},
    "extend_plan": {("plan", "extend"), ("plan", "select-batch"), ("plan", "rebudget"), ("chapter", "usage-record")},
    "story_review": {("review", "story-submit"), ("review", "story-evidence"), ("chapter", "usage-record")},
}


def stage_progressed(action: str, target: int, current: dict[str, Any]) -> bool:
    return current.get("chapter") == target and current.get("phase") in STAGE_PHASES.get(action, set())


def enforce_stage_command(active: dict[str, Any], args: Any, head: dict[str, Any], *, plan_fingerprint: str | None = None) -> None:
    if active.get("kind") != "stage" and not active.get("isolation_required"):
        return
    action = str(active.get("initial_action") or "")
    command = (args.cmd, getattr(args, args.cmd + "_cmd", None))
    if command not in STAGE_COMMANDS.get(action, set()):
        raise LedgerError("stage_capability_violation", "stage worker cannot advance or mutate another role", {"stage": action, "command": command})
    if (action == "assemble" and command == ("chapter", "submit")) or (action == "story_review" and command == ("review", "story-submit")):
        declared = active.get("output_path")
        supplied = getattr(args, "output", None)
        if not declared or not supplied or Path(declared).resolve() != Path(supplied).resolve():
            raise LedgerError("stage_capability_violation", "assemble submit must use the declared staging output_path")
    required_phase = {"draft": "await_draft", "polish": "await_polish", "assemble": "await_assembly", "ack": "await_ack"}.get(action)
    if command != ("chapter", "usage-record") and required_phase and head.get("phase") != required_phase:
        raise LedgerError("stage_capability_violation", "stage finished; only telemetry may be recorded before exiting")
    if action == "extend_plan" and command != ("chapter", "usage-record") and plan_fingerprint not in {active.get("plan_fingerprint"), active.get("rebudgeted_plan_fingerprint")}:
        raise LedgerError("stage_capability_violation", "plan job has changed the signed plan; exit after its one extension")
    if action == "extend_plan" and command == ("plan", "rebudget") and active.get("rebudgeted_plan_fingerprint"):
        raise LedgerError("stage_capability_violation", "one plan job may rebudget only once")
    if action == "story_review" and command != ("chapter", "usage-record") and head.get("phase") != "idle":
        raise LedgerError("stage_capability_violation", "narrative review only runs between acknowledged chapters")
    if action == "story_review" and command != ("chapter", "usage-record"):
        marker = head.get("last_story_review") or {}
        if marker.get("review_id") == active.get("review_id") and marker.get("input_hash") == active.get("review_input_hash"):
            raise LedgerError("stage_capability_violation", "story review submitted; exit the review session")
    chapter = getattr(args, "chapter", None)
    if chapter is not None and command != ("review", "story-evidence") and int(chapter) != int(active["target"]):
        raise LedgerError("stage_capability_violation", "stage worker cannot mutate another chapter")


def record_session_receipt(store: BookStore, job: dict[str, Any], result: dict[str, Any] | None) -> None:
    """Persist an idempotent receipt before releasing a stage job's fence."""
    if job.get("kind") != "stage" and not job.get("isolation_required"):
        return
    result = result or {}
    session_id = str(result.get("session_id") or "").strip()
    if (not session_id or result.get("job_id") != job["job_id"]
            or result.get("action") != job["initial_action"]
            or result.get("context_origin") != "empty"):
        raise LedgerError("stage_session_unverified", "stage result requires matching job_id/action, host session_id and context_origin=empty")
    path = store.run_dir / "stage-sessions.json"
    sessions = read_json(path) if path.exists() else {}
    if not isinstance(sessions, dict):
        raise LedgerError("stage_session_unverified", "stage session registry is malformed")
    previous = sessions.get(session_id)
    if previous is not None and not isinstance(previous, dict):
        raise LedgerError("stage_session_unverified", "stage session registry is malformed")
    if previous and previous.get("job_id") != job["job_id"]:
        raise LedgerError("stage_session_reused", "a stage session was reused for another job", {"session_id": session_id})
    if not previous:
        sessions[session_id] = {"job_id": job["job_id"], "action": job["initial_action"], "chapter": job["target"], "context_origin": "empty", "recorded_at": now_ts()}
        atomic_json(path, sessions)
