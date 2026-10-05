"""Mandatory, evidence-bearing volume and endgame reviews.

The machine verifies coverage, provenance and revision binding. Narrative judgments
belong to a fresh editorial session; a hash or a quoted sentence is not a literary
score. Receipts are invalidated when the reviewed text or its signed contract changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infra.store import BookStore, _volume_label, volume_entry_for
from ..infra.util import LedgerError, atomic_json, atomic_text, canonical_json, now_ts, ok, read_json, sha256_text
from ..ledger.ledger import read_events

SCHEMA = "novel-ledger.story-review.v1"
MAX_SAMPLE_CHAPTERS = 5
MAX_SUMMARIES = 24


def _receipts_path(store: BookStore):
    return store.editorial_dir / "story-reviews.json"


def receipts(store: BookStore) -> dict[str, Any]:
    path = _receipts_path(store)
    value = read_json(path) if path.exists() else {}
    if not isinstance(value, dict):
        raise LedgerError("invalid_story_reviews", "story review receipts must be an object")
    for key, receipt in value.items():
        if (not isinstance(key, str) or not isinstance(receipt, dict)
                or receipt.get("schema") != SCHEMA or receipt.get("scope") not in {"volume", "book"}
                or receipt.get("verdict") not in {"pass", "fix"}
                or not isinstance(receipt.get("checks"), list)
                or any(not isinstance(check, dict) or not isinstance(check.get("id"), str)
                       or not isinstance(check.get("reason"), str) or not check["reason"].strip()
                       or check.get("status") not in {"pass", "BLOCKER", "UNVERIFIABLE"}
                       for check in receipt["checks"])):
            raise LedgerError("invalid_story_reviews", "a stored narrative review receipt is malformed", {"review_id": key})
    return value


def _ranges(plan: dict[str, Any], through: int) -> list[dict[str, Any]]:
    chapters = sorted((c for c in plan.get("chapters") or [] if isinstance(c, dict)), key=lambda c: int(c.get("chapter") or 0))
    groups: list[dict[str, Any]] = []
    for chapter in chapters:
        number = int(chapter.get("chapter") or 0)
        if number <= 0:
            continue
        volume = _volume_label(chapter.get("volume", 1))
        if not groups or groups[-1]["volume"] != volume:
            groups.append({"volume": volume, "first": number, "last": number})
        else:
            groups[-1]["last"] = number
    return [g for g in groups if g["last"] <= through]


def _sample(numbers: list[int], limit: int) -> list[int]:
    if len(numbers) <= limit:
        return numbers
    return sorted({numbers[round(i * (len(numbers) - 1) / (limit - 1))] for i in range(limit)})


def at_volume_boundary(store: BookStore) -> bool:
    through = int(store.read_head().get("last_acked_ch") or 0)
    chapters = {int(c.get("chapter") or 0): c for c in store.load_plan().get("chapters") or []}
    return through in chapters and through + 1 in chapters and _volume_label(chapters[through].get("volume", 1)) != _volume_label(chapters[through + 1].get("volume", 1))


def _spec(store: BookStore, scope: str, first: int, through: int, volume: str | None = None) -> dict[str, Any]:
    from .extract import canon_source_fingerprint
    from .narrative_contract import narrative_contract_view
    from ..infra.volume_outline import VOLUME_OUTLINE_SCHEMA, volume_outline_errors, volume_outline_view

    plan = store.load_plan()
    volume_errors = volume_outline_errors(plan, required=store.load_config().get("volume_outline_schema") == VOLUME_OUTLINE_SCHEMA)
    if volume_errors:
        raise LedgerError("volume_outline_contract_violation", "restore the signed complete volume outline before narrative review", {"errors": volume_errors})
    compiled = read_json(store.kb_path) if store.kb_path.exists() else {}
    recorded_source = compiled.get("source_fingerprint") if isinstance(compiled, dict) else None
    current_source = canon_source_fingerprint(store.canon_dir)
    empty_source = "sha256:" + sha256_text("")
    if ((current_source not in {"", empty_source} or recorded_source)
            and current_source != recorded_source):
        raise LedgerError("story_review_canon_unsynced", "sync the current original canon before preparing or accepting a narrative review", {"compiled_source_fingerprint": recorded_source, "current_source_fingerprint": current_source})
    contract = narrative_contract_view(store, plan=plan)
    numbers = list(range(first, through + 1))
    hashes: dict[str, str] = {}
    for number in numbers:
        path = store.chapter_md_path(number)
        if not path.exists() or not store.ack_path(number).exists():
            raise LedgerError("story_review_missing_chapter", "review requires committed and acknowledged prose", {"chapter": number})
        prose = path.read_text(encoding="utf-8")
        digest = "sha256:" + sha256_text(prose.rstrip("\n"))
        ack = read_json(store.ack_path(number))
        if ack.get("verdict") != "pass" or ack.get("review_status") == "needs_review" or ack.get("prose_hash") not in {digest, "sha256:" + sha256_text(prose)}:
            raise LedgerError("story_review_unacked_revision", "review requires an acknowledged current revision", {"chapter": number})
        hashes[str(number)] = "sha256:" + sha256_text(prose)
    volume_meta = volume_entry_for(plan.get("volumes"), volume) or {} if volume else {}
    milestones = [m for m in (plan.get("book_outline") or {}).get("milestones", []) if isinstance(m, dict) and first <= int(m.get("chapter") or 0) <= through]
    checks = ["reader_promise", "character_choices_and_costs", "character_arc", "plot_payoffs", "clocks_and_world_rules"]
    if scope == "book":
        checks += ["main_arc_endgame", "long_term_commitments", "theme_and_core_relationships"]
        if plan.get("volume_outline_contract"):
            checks.append("volume_chain_endgame")
    else:
        if volume_meta.get("goal"):
            checks.append("volume_goal")
        if volume_meta.get("outline"):
            checks.append("volume_plot_and_handoff")
    # Bind only history at/before the reviewed boundary. Future chapters must not
    # invalidate every earlier volume receipt; retroactive edits and rulings do.
    events = [event for event in read_events(store) if int((event.get("effective_chapter") if event.get("type") == "governance" else event.get("chapter")) or 0) <= through]
    binding = {
        "scope": scope, "volume": volume, "first": first, "through": through,
        "contract": contract, "world_spine": plan.get("world_spine"),
        "chapters": [c for c in plan.get("chapters") or [] if first <= int(c.get("chapter") or 0) <= through],
        "volume_contract": volume_meta, "milestones": milestones, "prose_hashes": hashes,
        "volume_outline_contract": plan.get("volume_outline_contract"),
        "events_hash": sha256_text(canonical_json(events).decode("utf-8")),
        "kb_hash": sha256_text(canonical_json(store.load_kb()).decode("utf-8")),
    }
    if scope == "book":
        binding["book_outline"] = plan.get("book_outline")
        binding["volume_receipts"] = {key: value for key, value in receipts(store).items() if key.startswith("volume:")}
    return {"schema": SCHEMA, "review_id": f"{scope}:{volume or 'all'}:{first}-{through}",
            "scope": scope, "volume": volume, "first": first, "through": through,
            "input_hash": "sha256:" + sha256_text(canonical_json(binding).decode("utf-8")),
            "contract": contract, "volume_contract": volume_meta, "milestones": milestones,
            "volume_outlines": volume_outline_view(plan, volume),
            "required_checks": checks, "prose_hashes": hashes,
            "book_promises": ((plan.get("book_outline") or {}).get("long_term_commitments") or []) if scope == "book" else []}


def required_reviews(store: BookStore, *, completing: bool = False) -> list[dict[str, Any]]:
    head = store.read_head()
    through = int(head.get("last_acked_ch") or 0)
    if through <= 0:
        return []
    plan = store.load_plan()
    groups = _ranges(plan, through)
    all_numbers = [int(c.get("chapter") or 0) for c in plan.get("chapters") or []]
    max_planned = max(all_numbers, default=0)
    specs = []
    for group in groups:
        # A planning batch ending inside a volume is not a volume boundary.
        if group["last"] == max_planned and not completing:
            continue
        specs.append(_spec(store, "volume", group["first"], group["last"], group["volume"]))
    if completing:
        specs.append(_spec(store, "book", 1, through))
    return specs


def review_state(store: BookStore, spec: dict[str, Any]) -> dict[str, Any] | None:
    receipt = receipts(store).get(spec["review_id"])
    if isinstance(receipt, dict) and receipt.get("input_hash") == spec["input_hash"]:
        checks = receipt["checks"]
        if (receipt.get("scope") != spec["scope"] or receipt.get("first") != spec["first"]
                or receipt.get("through") != spec["through"]
                or len(checks) != len(spec["required_checks"])
                or {check.get("id") for check in checks} != set(spec["required_checks"])
                or any(check.get("status") not in {"pass", "BLOCKER", "UNVERIFIABLE"} for check in checks)
                or ((receipt["verdict"] == "pass") != all(check.get("status") == "pass" for check in checks))):
            raise LedgerError("invalid_story_reviews", "stored review does not cover its signed range and checks", {"review_id": spec["review_id"]})
        return receipt
    return None


def pending_review(store: BookStore, *, completing: bool = False) -> dict[str, Any] | None:
    for spec in required_reviews(store, completing=completing):
        receipt = review_state(store, spec)
        if receipt is None:
            return spec
        if receipt.get("verdict") != "pass":
            return {**spec, "blocked": True, "receipt": receipt}
    return None


def prepare_review(store: BookStore, spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("blocked"):
        return ok(action="story_review_blocked", stop=True, chapter=spec["through"], review_id=spec["review_id"],
                  findings=spec["receipt"]["checks"], hint="repair the cited chapters or signed contract; the changed inputs require a fresh review")
    numbers = list(range(spec["first"], spec["through"] + 1))
    selected = _sample(numbers, MAX_SAMPLE_CHAPTERS)
    examples = []
    for number in selected:
        path = store.staging_dir / f"story-review-prose-{number:04d}.txt"
        atomic_text(path, store.chapter_md_path(number).read_text(encoding="utf-8"))
        examples.append({"chapter": number, "path": str(path), "prose_hash": spec["prose_hashes"][str(number)]})
    summary_numbers = _sample(numbers, MAX_SUMMARIES)
    summaries = []
    for number in summary_numbers:
        path = store.summary_path(number)
        text = str(read_json(path).get("l1_summary") or "") if path.exists() else ""
        summaries.append({"chapter": number, "summary": text, "omitted_chars": 0,
                          "path": str(path)})
    view = {k: v for k, v in spec.items() if k != "prose_hashes"}
    previous = [{"review_id": key, "verdict": value.get("verdict"), "input_hash": value.get("input_hash"),
                 "arc_note": next((c["reason"] for c in value.get("checks", []) if c["id"] == "character_arc"), "")}
                for key, value in receipts(store).items() if key.startswith("volume:")] if spec["scope"] == "book" else []
    chosen_previous = [previous[i] for i in _sample(list(range(len(previous))), MAX_SUMMARIES)] if previous else []
    view.update({"selected_prose": examples, "chapter_summaries": summaries,
                 "prior_volume_reviews": chosen_previous,
                 "omitted": {"prose_chapters": len(numbers) - len(selected), "summaries": len(numbers) - len(summaries), "volume_review_details": len(previous) - len(chosen_previous)},
                 "evidence_note": "Read all selected prose and the complete selected volume outline. Summaries locate history, they do not prove execution. Use memory recall, plan volume-outline --volume N, context read and review story-evidence to load additional relevant sources in full; mark unavailable evidence UNVERIFIABLE. Theme/relationship checks may explain author-approved absence, with prose evidence; do not invent genre quotas."})
    view_path = store.staging_dir / "story-review-view.json"
    output_path = store.staging_dir / "story-review-output.json"
    atomic_json(view_path, view)
    return ok(action="story_review", stop=False, phase=store.read_head().get("phase"), chapter=spec["through"],
              review_id=spec["review_id"], input_hash=spec["input_hash"], review_pack_path=str(view_path),
              submit_output_path=str(output_path), role_card=str(Path(__file__).resolve().parents[3] / "agents/roles/story-reviewer.md"))


def submit_review(store: BookStore, output: dict[str, Any]) -> dict[str, Any]:
    view_path = store.staging_dir / "story-review-view.json"
    if not view_path.exists() or not isinstance(output, dict):
        raise LedgerError("invalid_story_review", "prepare the pending review before submitting")
    view = read_json(view_path)
    spec = _spec(store, view["scope"], int(view["first"]), int(view["through"]), view.get("volume"))
    if output.get("review_id") != spec["review_id"] or output.get("input_hash") != spec["input_hash"] or view.get("input_hash") != spec["input_hash"]:
        raise LedgerError("stale_story_review", "review text or signed contract changed; prepare a fresh review")
    if store.autopilot_active_job_path.exists():
        active = read_json(store.autopilot_active_job_path)
        if (not isinstance(active, dict) or active.get("initial_action") != "story_review"
                or active.get("review_id") != spec["review_id"]
                or active.get("review_input_hash") != spec["input_hash"]):
            raise LedgerError("story_review_job_mismatch", "review submission does not match the active isolated review job")
    head = store.read_head()
    if head.get("phase") != "idle" or int(head.get("last_committed_ch") or 0) != int(head.get("last_acked_ch") or 0):
        raise LedgerError("story_review_phase", "story review submission is only allowed between acknowledged chapters")
    checks = output.get("checks")
    if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("id"), str) for c in checks) or {c["id"] for c in checks} != set(spec["required_checks"]) or len(checks) != len(spec["required_checks"]):
        raise LedgerError("story_review_check_coverage", "each required narrative check needs one explicit judgment")
    seen_chapters: set[int] = set()
    for check in checks:
        if (check.get("status") not in {"pass", "BLOCKER", "UNVERIFIABLE"}
                or not isinstance(check.get("reason"), str) or not check["reason"].strip()):
            raise LedgerError("invalid_story_review", "checks require status and an evidence-based reason")
        evidence = check.get("evidence") or []
        if not isinstance(evidence, list) or (check["status"] == "pass" and not evidence):
            raise LedgerError("story_review_evidence_missing", "passing checks require direct prose evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or not isinstance(entry.get("chapter"), int) or isinstance(entry.get("chapter"), bool):
                raise LedgerError("invalid_story_review_evidence", "evidence requires an integer chapter")
            number = entry["chapter"]
            quote = entry.get("quote")
            if not spec["first"] <= number <= spec["through"] or not isinstance(quote, str) or len(quote) < 6 or quote not in store.chapter_md_path(number).read_text(encoding="utf-8"):
                raise LedgerError("invalid_story_review_evidence", "evidence must quote the reviewed current prose exactly")
            seen_chapters.add(number)
    passed = all(c["status"] == "pass" for c in checks)
    if passed and (spec["first"] not in seen_chapters or spec["through"] not in seen_chapters):
        raise LedgerError("story_review_evidence_distribution", "passing reviews need opening and ending prose evidence")
    state = receipts(store)
    previous = state.get(spec["review_id"])
    if isinstance(previous, dict) and previous.get("input_hash") == spec["input_hash"]:
        if previous.get("verdict") == "fix" and passed:
            raise LedgerError("story_review_blocked", "the current version has a failing review; repair its prose or signed contract before requesting a fresh judgment")
        identical = canonical_json(previous.get("checks")) == canonical_json(checks)
    else:
        identical = False
    # An identical retry can finish a interrupted HEAD write, but does not
    # replace its receipt timestamp or invalidate an already reviewed endgame.
    if not identical:
        state[spec["review_id"]] = {"schema": SCHEMA, "input_hash": spec["input_hash"], "scope": spec["scope"],
                                    "first": spec["first"], "through": spec["through"], "verdict": "pass" if passed else "fix",
                                    "checks": checks, "recorded_at": now_ts()}
        atomic_json(_receipts_path(store), state)
    head["last_story_review"] = {"review_id": spec["review_id"], "input_hash": spec["input_hash"], "through": spec["through"], "verdict": "pass" if passed else "fix"}
    head["updated_at"] = now_ts()
    store.write_head(head)
    return ok(action="story_review_submitted", review_id=spec["review_id"], verdict="pass" if passed else "fix", stop=True)


def export_evidence(store: BookStore, chapter: int) -> dict[str, Any]:
    view_path = store.staging_dir / "story-review-view.json"
    if not view_path.exists():
        raise LedgerError("story_review_not_prepared", "no story review view exists")
    view = read_json(view_path)
    if not int(view["first"]) <= chapter <= int(view["through"]):
        raise LedgerError("story_review_evidence_range", "requested chapter is outside this review")
    prose = store.chapter_md_path(chapter).read_text(encoding="utf-8")
    path = store.staging_dir / f"story-review-prose-{chapter:04d}.txt"
    atomic_text(path, prose)
    return ok(action="story_review_evidence", chapter=chapter, path=str(path), chars=len(prose))
