"""Author-approved narrative inputs shared by planning and story review.

New hatch contracts retain the declared fields verbatim.  Legacy books expose
source excerpts as unstructured evidence, never as a guessed author contract.
Relevant source sections stay complete and other sections remain retrievable.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import PurePath
from typing import Any

from ..infra.store import BookStore
from ..infra.util import LedgerError, atomic_json, canonical_json, now_ts, ok, read_json, sha256_bytes, sha256_text
from .extract import canon_source_fingerprint


NARRATIVE_CONTRACT_SCHEMA = "novel-ledger.narrative-contract.v1"
NARRATIVE_CONTRACT_VIEW_SCHEMA = "novel-ledger.narrative-contract-view.v1"
NARRATIVE_CONTRACT_SOURCE_SCHEMA = "novel-ledger.narrative-contract-sources.v1"
_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+([^\n]+)(?:\n|$)")
_REDLINE_HEADING = re.compile(r"红线|硬禁|禁则|hard[ _-]*constraints|red[ _-]*lines", re.I)
_PRIORITY_HEADING = re.compile(r"主线|终局|主角|动力|欲望|读者|定位|承诺|关系|对抗|主题|价值|规则|世界|机制|人物弧")


def validate_narrative_contract(value: Any) -> dict[str, Any]:
    """Validate a stored contract without normalizing, trimming, or inventing it."""
    if not isinstance(value, dict) or value.get("schema") != NARRATIVE_CONTRACT_SCHEMA:
        raise LedgerError("narrative_contract_invalid", f"narrative_contract must declare {NARRATIVE_CONTRACT_SCHEMA}")
    for key in ("reader_promise", "protagonist_engine", "main_arc", "world_rules"):
        if not isinstance(value.get(key), dict):
            raise LedgerError("narrative_contract_invalid", f"narrative_contract.{key} must be an object")
    for section, key in (("reader_promise", "selling_points"), ("protagonist_engine", "desire_deep"),
                         ("main_arc", "core_quest"), ("main_arc", "endgame_condition")):
        if not isinstance(value[section].get(key), str) or not value[section][key].strip():
            raise LedgerError("narrative_contract_invalid", f"narrative_contract.{section}.{key} must retain the explicit author wording")
    constraints = value.get("hard_constraints")
    if not isinstance(constraints, list) or any(not isinstance(item, str) or not item.strip() for item in constraints):
        raise LedgerError("narrative_contract_invalid", "narrative_contract.hard_constraints must be an explicit string list; [] means none declared")
    if "core_relationships" in value and not isinstance(value["core_relationships"], dict):
        raise LedgerError("narrative_contract_invalid", "narrative_contract.core_relationships must be an object when declared")
    baseline = value.get("source_baselines")
    if baseline is not None:
        if not isinstance(baseline, dict) or baseline.get("schema") != NARRATIVE_CONTRACT_SOURCE_SCHEMA:
            raise LedgerError("narrative_contract_invalid", "narrative_contract.source_baselines has an invalid schema")
        for key in ("intent", "outline", "canon"):
            if key not in baseline or (baseline[key] is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(baseline[key]))):
                raise LedgerError("narrative_contract_invalid", f"narrative_contract.source_baselines.{key} must be a source hash or explicit null")
    try:
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise LedgerError("narrative_contract_invalid", "narrative_contract must be JSON-compatible") from exc
    return copy.deepcopy(value)


def narrative_contract_source_baselines(store: BookStore) -> dict[str, Any]:
    """Bind authoritative editorial text and original canon, not compiled excerpts."""
    return {
        "schema": NARRATIVE_CONTRACT_SOURCE_SCHEMA,
        # Bind complete sources while loading only material needed by the task.
        "intent": "sha256:" + sha256_bytes(store.intent_path.read_bytes()) if store.intent_path.exists() else None,
        "outline": "sha256:" + sha256_bytes(store.outline_path.read_bytes()) if store.outline_path.exists() else None,
        "canon": canon_source_fingerprint(store.canon_dir) or None,
    }


def _contract_fingerprint(value: Any) -> str:
    return "sha256:" + sha256_text(canonical_json(value).decode("utf-8"))


def amend_narrative_contract(store: BookStore, contract: Any, *, actor: str, reason: str) -> dict[str, Any]:
    """Explicitly replace the full signed contract and bind the current sources.

    The caller holds the project write lock and authorizes this management action.
    No fields are inferred from sources and no outline, chapter or word budget is
    rewritten. Audit and contract replacement share one database transaction.
    """
    actor, reason = str(actor or "").strip(), str(reason or "").strip()
    if not actor or not reason:
        raise LedgerError("invalid_actor", "narrative contract amendment requires actor and reason")
    head = store.read_head()
    if (head.get("status") or "active") == "completed":
        raise LedgerError("book_completed", "reopen the book before amending its narrative contract")
    if head.get("phase") != "idle" or int(head.get("last_committed_ch") or 0) != int(head.get("last_acked_ch") or 0):
        raise LedgerError("narrative_contract_amend_inflight", "amend only at an idle, acknowledged chapter boundary")
    if not isinstance(contract, dict):
        raise LedgerError("narrative_contract_invalid", "amend requires the complete narrative contract JSON object")
    replacement = copy.deepcopy(contract)
    replacement["source_baselines"] = narrative_contract_source_baselines(store)
    replacement = validate_narrative_contract(replacement)
    compiled = read_json(store.kb_path) if store.kb_path.exists() else {}
    if not isinstance(compiled, dict):
        raise LedgerError("invalid_kb", "compiled canon must be an object before narrative contract amendment")
    if replacement["source_baselines"]["canon"] and compiled.get("source_fingerprint") != replacement["source_baselines"]["canon"]:
        raise LedgerError("narrative_contract_canon_unsynced", "run kb sync after importing the canon source, then explicitly amend the narrative contract")
    plan = store.load_plan()
    previous = plan.get("narrative_contract")
    path = store.editorial_dir / "narrative-contract-amendments.json"
    history = read_json(path) if path.exists() else []
    if not isinstance(history, list):
        raise LedgerError("narrative_contract_audit_invalid", "narrative contract amendment history must be a list")
    event = {"schema": "novel-ledger.narrative-contract-amendment.v1", "action": "plan.amend_contract",
             "actor": actor, "reason": reason, "ts": now_ts(),
             "through_chapter": int(head.get("last_acked_ch") or 0),
             "previous_contract": previous, "contract": replacement,
             "before_fingerprint": _contract_fingerprint(previous), "after_fingerprint": _contract_fingerprint(replacement)}
    plan["narrative_contract"] = replacement
    with store.transaction():
        store.save_plan(plan)
        atomic_json(path, [*history, event])
    return ok(action="plan_amend_contract", contract_fingerprint=event["after_fingerprint"],
              source_baselines=replacement["source_baselines"], audit_path=str(path),
              hint="the full contract is explicitly re-signed; prior story review receipts require fresh verification")


def _read_legacy_source(path: Any) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _redline_spans(text: str) -> list[tuple[int, int]]:
    headings = list(_HEADING.finditer(text))
    spans: list[tuple[int, int]] = []
    for index, heading in enumerate(headings):
        if not _REDLINE_HEADING.search(heading.group(2)):
            continue
        if spans and heading.start() < spans[-1][1]:
            continue
        end = next(
            (later.start() for later in headings[index + 1:] if len(later.group(1)) <= len(heading.group(1))),
            len(text),
        )
        spans.append((heading.start(), end))
    return spans


def _legacy_excerpt(text: str, spans: list[tuple[int, int]]) -> tuple[str, int]:
    """Load global narrative sections, leaving scene history for targeted reads."""
    headings = list(_HEADING.finditer(text))
    ranges: list[tuple[int, int]] = []
    if not headings:
        ranges.append((0, len(text)))
    elif headings[0].start() > 0:
        ranges.append((0, headings[0].start()))
    for index, heading in enumerate(headings):
        if not _PRIORITY_HEADING.search(heading.group(2)) or any(left <= heading.start() < right for left, right in [*spans, *ranges]):
            continue
        end = next((later.start() for later in headings[index + 1:] if len(later.group(1)) <= len(heading.group(1))), len(text))
        ranges.append((heading.start(), end))
    excerpt = "".join(text[start:end] for start, end in sorted(ranges))
    merged: list[list[int]] = []
    for start, end in sorted([*spans, *ranges]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return excerpt, max(0, len(text) - sum(end - start for start, end in merged))


def _source_index(text: str) -> list[dict[str, Any]]:
    return [{"section": match.group(2).strip(), "line": text.count("\n", 0, match.start()) + 1}
            for match in _HEADING.finditer(text)]


def read_narrative_source(store: BookStore, source: str, *, name: str | None = None,
                          section: str | None = None) -> dict[str, Any]:
    """Read one authoritative source or a complete named section on demand."""
    if source == "canon":
        relative = PurePath(name or "")
        if not name or relative.is_absolute() or ".." in relative.parts:
            raise LedgerError("invalid_context_source", "canon reads require a relative source name inside book/kb/canon")
        path = store.canon_dir / name
        if not path.resolve().is_relative_to(store.canon_dir.resolve()):
            raise LedgerError("invalid_context_source", "canon source must resolve inside book/kb/canon")
    elif source in {"intent", "outline"}:
        path = store.intent_path if source == "intent" else store.outline_path
    else:
        raise LedgerError("invalid_context_source", "source must be intent, outline or canon")
    if not path.is_file():
        raise LedgerError("context_source_missing", "requested narrative source does not exist", {"path": str(path)})
    original = _read_legacy_source(path)
    text = original
    if section is not None:
        headings = list(_HEADING.finditer(original))
        matches = [(i, h) for i, h in enumerate(headings) if h.group(2).strip() == section]
        if len(matches) != 1:
            raise LedgerError("context_section_ambiguous" if matches else "context_section_missing",
                              "request one exact, unique section heading", {"sections": _source_index(original)})
        index, heading = matches[0]
        end = next((h.start() for h in headings[index + 1:] if len(h.group(1)) <= len(heading.group(1))), len(original))
        text = original[heading.start():end]
    return ok(action="context_read", source=source, path=str(path), section=section,
              text=text, complete=True, source_hash="sha256:" + sha256_text(original), sections=_source_index(original))


def narrative_contract_view(store: BookStore, *, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return hashable relevant evidence for an empty planning/review conversation.

    Structured fields live in ``contract``.  Legacy ``sources[].text`` are exact
    source excerpts, with omissions and uncertainty explicitly disclosed.
    """
    plan = store.load_plan() if plan is None else plan
    stored = plan.get("narrative_contract")
    if stored is not None:
        contract = validate_narrative_contract(stored)
        current = narrative_contract_source_baselines(store)
        baseline = contract.get("source_baselines")
        if baseline is None:
            raise LedgerError("narrative_contract_source_unbound", "structured contract lacks source baselines; explicitly re-sign the complete JSON with plan amend-contract", {"source_baselines": current})
        changed = [key for key in ("intent", "outline", "canon") if baseline.get(key) != current[key]]
        if changed:
            raise LedgerError("narrative_contract_source_drift", "editorial or original canon sources changed after signing; import edited sources, run kb sync for canon changes, then explicitly re-sign the full JSON with plan amend-contract", {"changed_sources": changed, "signed": baseline, "current": current})
        view = {
            "schema": NARRATIVE_CONTRACT_VIEW_SCHEMA,
            "mode": "structured",
            "contract": contract,
            "sources": [{"kind": "plan", "path": str(store.plan_path), "field": "narrative_contract"}],
            "omitted": {"source_chars": 0, "canon_cards": 0},
            "limitations": [],
        }
    else:
        raw = [(kind, path, _read_legacy_source(path)) for kind, path in (
            ("intent", store.intent_path), ("outline", store.outline_path),
        )]
        sources: list[dict[str, Any]] = []
        for kind, path, text in raw:
            if kind == "intent" and text:
                # The author's intent may contain red lines without headings.
                # Keep it whole; excerpting it would silently lose such rules.
                sources.append({"kind": kind, "path": str(path), "text": text, "complete": True, "omitted_chars": 0})
                continue
            for start, end in _redline_spans(text):
                fragment = text[start:end]
                sources.append({"kind": "author_constraints", "path": str(path), "text": fragment, "complete": True, "omitted_chars": 0})
        cards = [card for card in store.load_kb() if isinstance(card, dict) and (
            str(card.get("hardness") or "") in {"hard", "硬"} or str(card.get("kind") or "") in {"rule", "world", "power_system"}
        )]
        protected_card_ids = set()
        seen_canon_sources = set()
        for card in cards:
            if not _REDLINE_HEADING.search(str(card.get("title") or "")):
                continue
            source = str(card.get("source") or "")
            relative = PurePath(source)
            source_path = store.canon_dir / source if source and not relative.is_absolute() and ".." not in relative.parts else None
            original = _read_legacy_source(source_path) if source_path is not None else ""
            original_spans = _redline_spans(original)
            if original_spans and source not in seen_canon_sources:
                seen_canon_sources.add(source)
                fragments = [original[start:end] for start, end in original_spans]
            elif original_spans:
                fragments = []
            else:
                fragments = [str(card.get("body") or "")]
            for fragment in fragments:
                sources.append({"kind": "author_constraints", "path": str(source_path or store.kb_path), "card_id": card.get("id"), "text": fragment, "complete": True, "omitted_chars": 0})
            protected_card_ids.add(str(card.get("id")))
        omitted_chars = 0
        # Global direction belongs in this view; scene history is indexed for
        # on-demand loading. Selected sections are never clipped by length.
        for kind, path, text in raw:
            if kind == "intent":
                continue
            excerpt, omitted = _legacy_excerpt(text, _redline_spans(text))
            omitted_chars += omitted
            if text:
                sources.append({"kind": kind, "path": str(path), "text": excerpt, "complete": omitted == 0, "omitted_chars": omitted})
        included = len(protected_card_ids)
        for card in cards:
            if str(card.get("id")) in protected_card_ids:
                continue
            body = str(card.get("body") or "")
            if body:
                sources.append({"kind": "canon", "path": str(store.kb_path), "card_id": card.get("id"), "text": body, "complete": True, "omitted_chars": 0})
                included += 1
        view = {
            "schema": NARRATIVE_CONTRACT_VIEW_SCHEMA,
            "mode": "legacy_unstructured",
            "contract": None,
            # Receipts also detect changes in sources deferred from this view.
            "source_baselines": narrative_contract_source_baselines(store),
            "sources": sources,
            "available_sources": [{"kind": kind, "path": str(path), "sections": _source_index(text)}
                                  for kind, path, text in raw if text],
            "load_on_demand": "context read --source intent|outline|canon [--name canon-file.md] [--section exact-heading] --project <project>",
            "omitted": {"source_chars": omitted_chars, "canon_cards": len(cards) - included},
            "limitations": [
                "旧书尚无结构化叙事合同；以上是原文来源，不能把摘录或模型归纳冒充作者签约字段。",
                "省略项非零或判据未声明时，应报告证据不足并定向补充；不能推断未入视图的承诺或规则不存在。",
            ],
        }
        if not sources:
            view["limitations"].append("意图书、大纲与适用正典来源均未提供；读者承诺、人物弧及终局条件未知。")
    view["fingerprint"] = "sha256:" + sha256_text(canonical_json(view).decode("utf-8"))
    return view
