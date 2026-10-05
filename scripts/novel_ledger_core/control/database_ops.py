"""Explicit migration, export and source import for SQLite projects."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..infra.artifact_path import ArtifactPath
from ..infra.database import BookDatabase
from ..infra.store import BookStore
from ..infra.util import LedgerError, ok


def migrate(store: BookStore) -> dict:
    if store.database.exists():
        return ok(action="database_migrate", already_migrated=True, **{k: v for k, v in store.database.integrity().items() if k != "ok"})
    root = Path(str(store.book))
    if not (root / "config.json").is_file() or not (root / "run" / "HEAD.json").is_file():
        raise LedgerError("not_initialized", "migration requires a file project with config and HEAD")
    if (root / "run" / "AUTOPILOT_ACTIVE_JOB.json").exists():
        raise LedgerError("migration_worker_active", "stop the old unattended worker before migrating")
    sources = []
    try:
        legacy_head = json.loads((root / "run" / "HEAD.json").read_text(encoding="utf-8"))
        if not isinstance(legacy_head, dict):
            raise ValueError("HEAD must be an object")
    except (ValueError, UnicodeDecodeError) as exc:
        raise LedgerError("migration_invalid_document", "repair invalid HEAD before migration", str(exc)) from exc
    active_chapter = int(legacy_head.get("chapter") or 0)
    phase = legacy_head.get("phase")
    accepted = set()
    if phase in {"await_polish", "await_assembly", "submitted", "await_ack"}:
        accepted.add(f"staging/draft-{active_chapter:04d}.txt")
    if phase in {"await_assembly", "submitted", "await_ack"}:
        accepted.add(f"staging/polished-{active_chapter:04d}.txt")
    for source in sorted(root.rglob("*")):
        if source.is_symlink():
            raise LedgerError("migration_symlink", "migration does not follow project symlinks", str(source))
        key = source.relative_to(root).as_posix()
        if not source.is_file() or (not ArtifactPath(source).managed and key not in accepted):
            continue
        body = source.read_bytes()
        try:
            text = body.decode("utf-8")
            if source.suffix == ".json":
                json.loads(text)
            elif source.suffix == ".jsonl":
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise LedgerError("migration_invalid_document", "repair invalid source before migration", {"path": key, "error": str(exc)}) from exc
        sources.append((key, body))
    # A failed migration never installs a partially populated database.
    pending = BookDatabase(root)
    pending.path = root / ("novel.sqlite3.migrate-" + uuid.uuid4().hex)
    try:
        with pending.transaction():
            for key, body in sources:
                pending.write(key, body)
            legacy_config = json.loads(pending.read("config.json"))
            from ..infra.store import SCHEMA_VERSION as CONFIG_SCHEMA_VERSION, config_schema_version
            if config_schema_version(legacy_config) != CONFIG_SCHEMA_VERSION:
                raise LedgerError("unsupported_schema", "migration requires the current config schema")
            ref = str(legacy_config.get("voice_anchor_file") or "").strip()
            if ref:
                anchor = Path(ref)
                if not anchor.is_absolute():
                    anchor = store.project / anchor
                if anchor.is_file():
                    pending.write("memory/voice-anchor.txt", anchor.read_bytes())
                    legacy_config["voice_anchor_file"] = "book/memory/voice-anchor.txt"
                    pending.write("config.json", json.dumps(legacy_config, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            from ..ledger.ledger import EMPTY_SNAPSHOT, _coerce_snapshot, apply_event, event_chain_issues
            import copy
            events = [json.loads(line) for line in pending.read("ledger/events.jsonl").splitlines() if line.strip()] if pending.has("ledger/events.jsonl") else []
            issues = event_chain_issues(events)
            if issues:
                raise LedgerError("migration_event_chain_broken", "repair event chain before migration", issues)
            snapshot = copy.deepcopy(EMPTY_SNAPSHOT)
            for event in events:
                snapshot = apply_event(snapshot, event)
            if pending.has("ledger/snapshot.json"):
                original = _coerce_snapshot(json.loads(pending.read("ledger/snapshot.json")))
                if original != snapshot:
                    raise LedgerError("migration_snapshot_drift", "repair file snapshot from events before migration")
        if not pending.integrity()["ok"]:
            raise LedgerError("migration_integrity_failed", "temporary database failed integrity checks")
        os.replace(pending.path, store.database_path)
    finally:
        pending.path.unlink(missing_ok=True)
    return ok(action="database_migrate", documents=len(sources), database_path=str(store.database_path), source_files="retained_as_exports")


def export_project(store: BookStore, destination: Path | None = None) -> dict:
    store.load_config()
    count = store.database.export(destination=destination)
    return ok(action="database_export", documents=count, destination=str(destination or store.book))


def import_source(store: BookStore, source: Path, *, kind: str) -> dict:
    store.load_config()
    source = source.resolve()
    if kind == "anchor":
        if not source.is_file():
            raise LedgerError("invalid_source", "anchor source must be a file")
        (store.memory_dir / "voice-anchor.txt").write_bytes(source.read_bytes())
        cfg = store.load_config()
        cfg["voice_anchor_file"] = "book/memory/voice-anchor.txt"
        store.save_config(cfg)
        return ok(action="database_import_source", kind=kind, documents=1)
    if kind in {"intent", "outline"}:
        if not source.is_file():
            raise LedgerError("invalid_source", "source must be a UTF-8 file")
        target = store.intent_path if kind == "intent" else store.outline_path
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return ok(action="database_import_source", kind=kind, documents=1)
    if not source.is_dir():
        raise LedgerError("invalid_source", "canon source must be a directory")
    from ..content.extract import SCAN_SUFFIXES
    files = [p for p in sorted(source.rglob("*")) if p.is_file() and p.suffix.lower() in SCAN_SUFFIXES]
    if not files:
        raise LedgerError("invalid_source", "canon source has no supported files")
    for file in files:
        try:
            file.resolve().relative_to(source)
        except ValueError:
            raise LedgerError("invalid_source", "canon source must remain inside its directory")
        if file.is_symlink():
            raise LedgerError("invalid_source", "canon source must remain inside its directory")
        text = file.read_text(encoding="utf-8")
        (store.canon_dir / file.relative_to(source)).write_text(text, encoding="utf-8")
    # Compile the newly imported authoritative canon in the same transaction.
    from ..content.extract import extract_to_file
    extract_to_file(store.canon_dir, store.kb_path, excerpt_max=0)
    return ok(action="database_import_source", kind=kind, documents=len(files))
