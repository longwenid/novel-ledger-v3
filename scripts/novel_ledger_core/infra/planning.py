"""Bounded candidate batches and their immutable expansion proofs."""
from __future__ import annotations

from typing import Any

from .util import LedgerError, canonical_json, sha256_text

MAX_BATCH_CHAPTERS = 20


def candidate_rows(candidate: dict[str, Any], start: int, end: int) -> list[dict[str, Any]]:
    rows = candidate.get("chapters")
    expected = list(range(start, end + 1))
    if (not isinstance(rows, list) or len(rows) != len(expected)
            or any(not isinstance(row, dict) for row in rows)
            or [row.get("chapter") for row in rows] != expected
            or any(isinstance(row.get("chapter"), bool) or not isinstance(row.get("chapter"), int) for row in rows)):
        raise LedgerError("plan_candidates_chapter_range", "every candidate must cover the exact, ordered, continuous batch chapter range")
    normalized = []
    for row in rows:
        for field in ("goal", "conflict", "outcome"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise LedgerError("invalid_plan", f"candidate chapter {row['chapter']} requires {field}")
        tags = row.get("tags") or []
        settles = row.get("settles") or []
        if (not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
                or not isinstance(settles, list) or any(not isinstance(hook, dict) or not str(hook.get("id") or "").strip() for hook in settles)):
            raise LedgerError("invalid_plan", "candidate tags and settlement ids must be lists")
        ids = [str(hook["id"]) for hook in settles]
        if len(set(ids)) != len(ids):
            raise LedgerError("invalid_plan", "candidate settlement ids must be unique within each chapter")
        normalized.append({"chapter": row["chapter"], **{field: row[field].strip() for field in ("goal", "conflict", "outcome")},
                           "tags": tags, "settles": sorted(ids)})
    return normalized


def rows_hash(rows: list[dict[str, Any]]) -> str:
    return sha256_text(canonical_json(rows).decode("utf-8"))


def expansion_matches(rows: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> bool:
    if len(rows) != len(chapters):
        return False
    for row, chapter in zip(rows, chapters):
        if (not isinstance(row, dict) or not isinstance(chapter, dict)
                or any(field not in row for field in ("chapter", "goal", "conflict", "outcome", "tags", "settles"))
                or not isinstance(row["tags"], list) or not isinstance(row["settles"], list)):
            return False
        if row["chapter"] != chapter.get("chapter"):
            return False
        if any(str(chapter.get(field) or "").strip() != row[field] for field in ("goal", "conflict", "outcome")):
            return False
        if list(chapter.get("tags") or []) != row["tags"]:
            return False
        paid_ids = set()
        for beat in chapter.get("beats") or []:
            if not isinstance(beat, dict):
                return False
            effects = beat.get("effects") or {}
            if not isinstance(effects, dict) or not isinstance(effects.get("hooks", []), list):
                return False
            paid_ids.update(str(hook["id"]) for hook in effects.get("hooks", [])
                            if isinstance(hook, dict) and hook.get("id") and hook.get("status") == "paid")
        paid = sorted(paid_ids)
        if paid != row["settles"]:
            return False
    return True


def expansion_diff(rows: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选骨架 vs 实际扩章的逐章差异（供 plan_batch_selection_mismatch 定位，最多 5 条）。

    实测教训：报错只有一句话时，plan worker 只能翻 skill 源码、手写 diff 脚本，
    再派一个修复 worker 重封存——差异本身进回执，修复就是照抄对齐。
    """
    diffs: list[dict[str, Any]] = []
    if len(rows) != len(chapters):
        diffs.append({
            "kind": "chapter_count",
            "expected": len(rows),
            "actual": len(chapters),
        })
    for row, chapter in zip(rows, chapters):
        if len(diffs) >= 5:
            break
        if not isinstance(row, dict) or not isinstance(chapter, dict):
            continue
        ch = row.get("chapter")
        if row.get("chapter") != chapter.get("chapter"):
            diffs.append({"kind": "chapter_number", "expected": row.get("chapter"), "actual": chapter.get("chapter")})
            continue
        for field in ("goal", "conflict", "outcome"):
            expected_text = str(row.get(field) or "")
            actual_text = str(chapter.get(field) or "").strip()
            if actual_text != expected_text:
                diffs.append({
                    "kind": "field_mismatch",
                    "chapter": ch,
                    "field": field,
                    "expected": expected_text[:80],
                    "actual": actual_text[:80],
                })
        if list(chapter.get("tags") or []) != row.get("tags"):
            diffs.append({
                "kind": "tags_mismatch",
                "chapter": ch,
                "expected": row.get("tags"),
                "actual": list(chapter.get("tags") or []),
            })
        paid_ids = set()
        for beat in chapter.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            effects = beat.get("effects") or {}
            if not isinstance(effects, dict):
                continue
            for hook in effects.get("hooks", []) or []:
                if isinstance(hook, dict) and hook.get("id") and hook.get("status") == "paid":
                    paid_ids.add(str(hook["id"]))
        settles = list(row.get("settles") or [])
        if sorted(paid_ids) != settles:
            diffs.append({
                "kind": "settles_mismatch",
                "chapter": ch,
                "unpaid": [h for h in settles if h not in paid_ids],
                "extra_paid": sorted(paid_ids - set(settles)),
            })
    return diffs[:5]


def expansion_selection(events: list[dict[str, Any]], chapters: list[dict[str, Any]], *, required: bool) -> dict[str, Any] | None:
    if not chapters or max(int(c["chapter"]) for c in chapters) <= 3:
        return None
    start, end = int(chapters[0]["chapter"]), int(chapters[-1]["chapter"])
    selections = [event for event in events if isinstance(event, dict) and event.get("type") == "governance"
                  and event.get("action") == "plan.batch_select"
                  and event.get("batch_from") == start and event.get("batch_to") == end]
    if not selections:
        if required:
            raise LedgerError("plan_batch_selection_missing", "select a candidate for this exact phase range before extending")
        return None
    event = selections[-1]
    rows = event.get("selected_chapters")
    if (not isinstance(rows, list) or rows_hash(rows) != event.get("candidate_hash")
            or not expansion_matches(rows, chapters)):
        details = None
        if isinstance(rows, list):
            details = {"diff": expansion_diff(rows, chapters),
                       "hint": "align the expanded chapters to the selected candidate per the diff"
                               " (copy goal/conflict/outcome/tags verbatim; place each settles id as a paid hook in that chapter's effects.hooks)"}
        raise LedgerError("plan_batch_selection_mismatch", "expanded chapters must retain the selected goals, conflicts, outcomes, tags and same-chapter paid hooks", details)
    return {"batch_from": start, "batch_to": end, "selected_id": event["selected_id"],
            "selection_hash": event["hash"], "candidate_hash": event["candidate_hash"],
            "chapters_hash": rows_hash(chapters)}


