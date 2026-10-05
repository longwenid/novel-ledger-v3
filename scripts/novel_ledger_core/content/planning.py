"""Editorial batch proof checks over persistent ledger events."""
from __future__ import annotations

from typing import Any

from ..infra.util import LedgerError
from ..infra.planning import MAX_BATCH_CHAPTERS, expansion_selection as _expansion_selection
from ..ledger.ledger import read_events


def expansion_selection(store: Any, chapters: list[dict[str, Any]], *, required: bool):
    return _expansion_selection(read_events(store), chapters, required=required)


def batch_selection_coverage(store: Any, *, after_chapter: int) -> dict[str, Any]:
    """Check each actual phase/expansion, never a broad historical interval alone."""
    plan = store.load_plan()
    chapters = {int(c["chapter"]): c for c in plan.get("chapters") or [] if isinstance(c, dict)}
    ranges = {(int(p["chapter_start"]), int(p["chapter_end"])) for p in plan.get("phases") or []
              if isinstance(p, dict) and p.get("chapter_start") and p.get("chapter_end")}
    ranges.update((int(b["batch_from"]), int(b["batch_to"])) for b in plan.get("expansion_batches") or [] if isinstance(b, dict))
    # Legacy manually signed plans have no phases; their actual remaining range
    # still cannot be covered by an unrelated, over-wide selection.
    if not ranges:
        remaining = {n for n in chapters if n > max(after_chapter, 3)}
        for event in read_events(store):
            if event.get("action") != "plan.batch_select":
                continue
            start, end = int(event.get("batch_from") or 0), int(event.get("batch_to") or 0)
            if 0 < start <= end and end - start + 1 <= MAX_BATCH_CHAPTERS and remaining.intersection(range(start, end + 1)):
                ranges.add((start, end))
                remaining.difference_update(range(start, end + 1))
        if remaining:
            ordered = sorted(remaining)
            start = previous = ordered[0]
            for number in ordered[1:]:
                if number != previous + 1 or number - start >= MAX_BATCH_CHAPTERS:
                    ranges.add((start, previous))
                    start = number
                previous = number
            ranges.add((start, previous))
    issues = []
    for start, end in sorted(ranges):
        if end <= after_chapter or end <= 3:
            continue
        batch = [chapters[n] for n in range(start, end + 1) if n in chapters]
        try:
            if end - start + 1 > MAX_BATCH_CHAPTERS or len(batch) != end - start + 1:
                raise LedgerError("plan_batch_selection_mismatch", "actual batch range must be complete and at most 20 chapters")
            proof = expansion_selection(store, batch, required=True)
            recorded = next((b for b in plan.get("expansion_batches") or []
                             if b.get("batch_from") == start and b.get("batch_to") == end), None)
            if recorded and proof != recorded:
                raise LedgerError("plan_batch_selection_mismatch", "expanded chapter proof no longer matches the signed batch")
        except LedgerError as exc:
            issues.append({"code": exc.code, "from_chapter": start, "through_chapter": end})
    return {"issues": issues, "selection_missing_from": issues[0]["from_chapter"] if issues else None}
