"""Durable editorial findings: observations survive staging cleanup and retries.

An append-only journal is the source; listing and checkpoint summaries are bounded
projections. Dispositions are explicit events, never inferred from an ack quote.
"""
from __future__ import annotations

import json
from typing import Any

from ..infra.store import BookStore
from ..infra.util import LedgerError, canonical_json, sha256_text, now_ts, atomic_json, ok


def _path(store: BookStore):
    return store.editorial_dir / "findings.jsonl"


def _append(store: BookStore, event: dict[str, Any]) -> None:
    path = _path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    from ..infra.util import append_bytes
    append_bytes(path, canonical_json({"schema": "novel-ledger.finding.v1", "ts": now_ts(), **event}))


def findings_state(store: BookStore) -> dict[str, dict[str, Any]]:
    path = _path(store)
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("schema") != "novel-ledger.finding.v1":
                raise ValueError("invalid finding event")
            identity = event["id"]
            if event["event"] == "observed":
                out.setdefault(identity, {**event, "disposition": "open"})
            elif event["event"] == "disposed" and identity in out:
                out[identity].update({"disposition": event["disposition"], "ruling": event["reason"], "actor": event["actor"], "disposed_at": event["ts"]})
            else:
                raise ValueError("unknown event or finding")
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerError("invalid_findings_journal", "editorial findings journal cannot be replayed", {"line": number}) from exc
    return out


def record_findings(store: BookStore, chapter: int, prose: str, findings: list[dict[str, Any]]) -> None:
    existing = findings_state(store)
    prose_hash = "sha256:" + sha256_text(prose)
    for finding in findings:
        body = {"chapter": chapter, "prose_hash": prose_hash, **finding}
        identity = "finding:" + sha256_text(canonical_json(body).decode("utf-8"))[:24]
        if identity not in existing:
            _append(store, {"event": "observed", "id": identity, **body})
            existing[identity] = {"id": identity, **body, "disposition": "open"}
    # The next drafting/assembly session receives only this chapter's unresolved
    # findings; the global journal is never loaded into a model context.
    pending = [f for f in existing.values() if f["chapter"] == chapter and f.get("disposition") == "open"]
    atomic_json(store.staging_dir / f"review-findings-{chapter:04d}.json", {"chapter": chapter, "findings": pending[:50], "omitted": max(0, len(pending) - 50)})


def list_findings(store: BookStore, *, chapter: int | None = None, limit: int = 50) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise LedgerError("invalid_args", "finding limit must be between 1 and 200")
    pending = [f for f in findings_state(store).values() if f.get("disposition") == "open" and (chapter is None or f["chapter"] == chapter)]
    return ok(action="review_list", findings=pending[:limit], pending_count=len(pending), omitted=max(0, len(pending) - limit), journal_path=str(_path(store)))


def resolve_finding(store: BookStore, identity: str, *, disposition: str, actor: str, reason: str) -> dict[str, Any]:
    if disposition not in {"accepted", "deferred", "closed"} or not actor.strip() or not reason.strip():
        raise LedgerError("invalid_finding_disposition", "disposition, actor and reason are required")
    current = findings_state(store).get(identity)
    if current is None:
        raise LedgerError("unknown_finding", "finding id does not exist", identity)
    if current.get("disposition") == disposition and current.get("ruling") == reason and current.get("actor") == actor:
        return ok(action="review_resolved", id=identity, disposition=disposition, idempotent=True)
    _append(store, {"event": "disposed", "id": identity, "disposition": disposition, "actor": actor, "reason": reason})
    return ok(action="review_resolved", id=identity, disposition=disposition)


def review_summary(store: BookStore) -> dict[str, Any]:
    pending = [f for f in findings_state(store).values() if f.get("disposition") == "open"]
    return {"pending_count": len(pending), "by_severity": {s: sum(f.get("severity") == s for f in pending) for s in ("BLOCKER", "WARNING", "NIT", "UNVERIFIABLE")}, "journal_path": str(_path(store))}
