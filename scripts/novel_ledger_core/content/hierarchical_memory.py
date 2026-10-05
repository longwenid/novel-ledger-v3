"""Deterministic hierarchical narrative memory built from existing L1 summaries.

No model call is made here. Chapter summaries remain complete in persistent
layers. A chapter loads its current phase and source indexes for further recall,
instead of receiving the complete history of the book.
"""

from __future__ import annotations

from typing import Any

from ..infra.store import BookStore, _volume_label
from ..infra.util import LedgerError, atomic_json, canonical_json, read_json, sha256_bytes

MEMORY_SCHEMA = "novel-ledger.hierarchy.v1"
AUTO_PHASE_CHAPTERS = 20


def _empty_memory() -> dict[str, Any]:
    return {
        "schema": MEMORY_SCHEMA,
        "updated_chapter": 0,
        "phases": [],
        "volumes": [],
        "book": {"volume_ids": [], "summary": ""},
        "fingerprint": "",
    }


def _with_fingerprint(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    payload.pop("fingerprint", None)
    data["fingerprint"] = "sha256:" + sha256_bytes(canonical_json(payload))
    return data


def load_hierarchical_memory(store: BookStore) -> dict[str, Any]:
    path = store.hierarchical_memory_path
    if not path.exists():
        return _with_fingerprint(_empty_memory())
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema") != MEMORY_SCHEMA:
        raise LedgerError("invalid_hierarchical_memory", f"invalid hierarchy file: {path}")
    if not isinstance(data.get("phases"), list) or not isinstance(data.get("volumes"), list):
        raise LedgerError("invalid_hierarchical_memory", "hierarchy phases/volumes must be lists")
    if not isinstance(data.get("book"), dict):
        raise LedgerError("invalid_hierarchical_memory", "hierarchy book must be an object")
    expected = _with_fingerprint({**data, "fingerprint": ""})["fingerprint"]
    if data.get("fingerprint") != expected:
        raise LedgerError("invalid_hierarchical_memory", "hierarchy fingerprint mismatch")
    return data


def _full_lines(lines: list[str]) -> str:
    """Join complete selected records without sampling or character clipping."""
    return "\n".join(str(line).strip() for line in lines if str(line).strip())


def _chapter_descriptors(plan: dict[str, Any]) -> dict[int, dict[str, str]]:
    phase_names = {
        str(item.get("id") or "").strip(): str(item.get("name") or item.get("id") or "").strip()
        for item in (plan.get("phases") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    rows: list[tuple[int, str, str]] = []
    for raw in plan.get("chapters") or []:
        if not isinstance(raw, dict):
            continue
        try:
            chapter = int(raw.get("chapter") or 0)
        except (TypeError, ValueError):
            continue
        if chapter <= 0:
            continue
        volume = _volume_label(raw.get("volume", 1))
        phase_id = str(raw.get("phase_id") or "").strip()
        rows.append((chapter, volume, phase_id))
    rows.sort()

    volume_positions: dict[str, int] = {}
    descriptors: dict[int, dict[str, str]] = {}
    for chapter, volume, phase_id in rows:
        position = volume_positions.get(volume, 0) + 1
        volume_positions[volume] = position
        auto_index = (position - 1) // AUTO_PHASE_CHAPTERS + 1
        descriptors[chapter] = {
            "volume_id": volume,
            "phase_label": phase_names.get(phase_id, phase_id) or f"自动阶段{auto_index}",
            "phase_source": "plan" if phase_id else "auto",
            "story_phase_id": phase_id,
        }
    return descriptors


def _phase_summary(phase: dict[str, Any]) -> str:
    lines = [
        f"第{int(item.get('chapter') or 0)}章：{str(item.get('summary') or '').strip()}"
        for item in phase.get("chapters") or []
        if isinstance(item, dict) and str(item.get("summary") or "").strip()
    ]
    return _full_lines(lines)


def _volume_summary(volume: dict[str, Any], phases_by_id: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for phase_id in volume.get("phase_ids") or []:
        phase = phases_by_id.get(str(phase_id))
        if not phase:
            continue
        lines.append(
            f"{phase.get('label')}（第{phase.get('start_chapter')}-{phase.get('end_chapter')}章）："
            f"{phase.get('summary') or ''}"
        )
    return _full_lines(lines)


def _book_history(volumes: list[dict[str, Any]]) -> str:
    lines = [
        f"{volume.get('id')}（第{volume.get('start_chapter')}-{volume.get('end_chapter')}章）："
        f"{volume.get('summary') or ''}"
        for volume in volumes
    ]
    return _full_lines(lines)


def _append_summary(
    data: dict[str, Any],
    *,
    chapter: int,
    summary: str,
    descriptor: dict[str, str],
) -> None:
    phases = data["phases"]
    volumes = data["volumes"]
    volume_id = descriptor["volume_id"]
    phase_label = descriptor["phase_label"]
    story_phase_id = descriptor.get("story_phase_id") or ""

    phase: dict[str, Any] | None = None
    if phases:
        latest = phases[-1]
        if (
            latest.get("volume_id") == volume_id
            and latest.get("label") == phase_label
            and str(latest.get("story_phase_id") or "") == story_phase_id
            and int(latest.get("end_chapter") or 0) == chapter - 1
        ):
            phase = latest
    if phase is None:
        phase = {
            "id": f"{volume_id}:phase-{len(phases) + 1:04d}",
            "volume_id": volume_id,
            "label": phase_label,
            "source": descriptor["phase_source"],
            "story_phase_id": story_phase_id,
            "start_chapter": chapter,
            "end_chapter": chapter,
            "chapter_count": 0,
            "chapters": [],
            "summary": "",
        }
        phases.append(phase)
    phase["chapters"].append({"chapter": chapter, "summary": summary})
    phase["end_chapter"] = chapter
    phase["chapter_count"] = len(phase["chapters"])
    phase["summary"] = _phase_summary(phase)

    volume = next((item for item in volumes if item.get("id") == volume_id), None)
    if volume is None:
        volume = {
            "id": volume_id,
            "start_chapter": chapter,
            "end_chapter": chapter,
            "chapter_count": 0,
            "phase_ids": [],
            "summary": "",
        }
        volumes.append(volume)
    if phase["id"] not in volume["phase_ids"]:
        volume["phase_ids"].append(phase["id"])
    volume["end_chapter"] = chapter
    volume["chapter_count"] = int(volume.get("chapter_count") or 0) + 1
    phases_by_id = {str(item.get("id")): item for item in phases}
    volume["summary"] = _volume_summary(volume, phases_by_id)

    data["book"] = {
        "volume_ids": [str(item.get("id")) for item in volumes],
        "chapter_count": sum(int(item.get("chapter_count") or 0) for item in volumes),
        "start_chapter": min((int(item.get("start_chapter") or chapter) for item in volumes), default=chapter),
        "end_chapter": chapter,
        "summary": _book_history(volumes),
    }
    data["updated_chapter"] = chapter


def rebuild_hierarchical_memory(store: BookStore, through_chapter: int) -> dict[str, Any]:
    """Rebuild the derived hierarchy from chapter L1 files; no prose or model call is used."""
    through = max(0, int(through_chapter))
    data = _empty_memory()
    descriptors = _chapter_descriptors(store.load_plan())
    for chapter in range(1, through + 1):
        path = store.summary_path(chapter)
        if not path.exists():
            continue
        raw = read_json(path)
        summary = str(raw.get("l1_summary") or "").strip() if isinstance(raw, dict) else ""
        descriptor = descriptors.get(chapter)
        if summary and descriptor:
            _append_summary(data, chapter=chapter, summary=summary, descriptor=descriptor)
    _with_fingerprint(data)
    atomic_json(store.hierarchical_memory_path, data)
    return data


def update_hierarchical_memory(store: BookStore, chapter: int, l1_summary: str) -> dict[str, Any]:
    """Increment the hierarchy after commit, rebuilding only when sequence state is inconsistent."""
    chapter = int(chapter)
    summary = str(l1_summary or "").strip()
    if chapter <= 0 or not summary:
        raise LedgerError("invalid_hierarchical_memory", "chapter and l1_summary are required")
    data = load_hierarchical_memory(store)
    if int(data.get("updated_chapter") or 0) != chapter - 1:
        return rebuild_hierarchical_memory(store, chapter)
    descriptor = _chapter_descriptors(store.load_plan()).get(chapter)
    if descriptor is None:
        raise LedgerError("invalid_plan", f"chapter {chapter} has no hierarchy descriptor")
    _append_summary(data, chapter=chapter, summary=summary, descriptor=descriptor)
    _with_fingerprint(data)
    atomic_json(store.hierarchical_memory_path, data)
    return data


def _book_seed(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    for label, value in (
        ("书名", plan.get("title")),
        ("世界脊柱", plan.get("world_spine")),
        ("全书卷脊", plan.get("volume_spine")),
    ):
        text = str(value or "").strip()
        if text:
            lines.append(f"{label}：{text}")
    outline = plan.get("book_outline")
    if isinstance(outline, dict):
        for act in outline.get("acts") or []:
            if not isinstance(act, dict):
                continue
            name = str(act.get("name") or act.get("act") or act.get("stage") or "").strip()
            goal = str(act.get("goal") or act.get("purpose") or act.get("summary") or "").strip()
            if name or goal:
                lines.append(f"全书阶段{name}：{goal}".rstrip("："))
    return _full_lines(lines)


def _memory_through(data: dict[str, Any], cutoff_chapter: int) -> dict[str, Any]:
    """Return an in-memory hierarchy containing only chapters through ``cutoff``.

    The usual next-chapter path already has exactly that state and returns the
    loaded object unchanged.  The filtered rebuild matters during crash recovery:
    a chapter may have reached the derived hierarchy before HEAD advances, but its
    own output must never become part of its input fingerprint.
    """
    cutoff = max(0, int(cutoff_chapter))
    if int(data.get("updated_chapter") or 0) <= cutoff:
        return data
    sliced = _empty_memory()
    for phase in data.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        descriptor = {
            "volume_id": str(phase.get("volume_id") or ""),
            "phase_label": str(phase.get("label") or ""),
            "phase_source": str(phase.get("source") or "auto"),
            "story_phase_id": str(phase.get("story_phase_id") or ""),
        }
        if not descriptor["volume_id"] or not descriptor["phase_label"]:
            continue
        for item in phase.get("chapters") or []:
            if not isinstance(item, dict):
                continue
            item_chapter = int(item.get("chapter") or 0)
            summary = str(item.get("summary") or "").strip()
            if 0 < item_chapter <= cutoff and summary:
                _append_summary(
                    sliced,
                    chapter=item_chapter,
                    summary=summary,
                    descriptor=descriptor,
                )
    return _with_fingerprint(sliced)


def memory_for_pack(store: BookStore, chapter: int) -> dict[str, Any]:
    """Load complete current-phase material, with indexes for other history.

    Old stored layer summaries may have been clipped by previous releases.
    Rebuild the selected phase from its complete chapter records when reading.
    """
    data = _memory_through(load_hierarchical_memory(store), int(chapter) - 1)
    if int(data.get("updated_chapter") or 0) <= 0:
        seed = _book_seed(store.load_plan())
        return {"schema": MEMORY_SCHEMA, "book_spine": {"summary": seed}} if seed else {}

    plan = store.load_plan()
    descriptor = _chapter_descriptors(plan).get(int(chapter), {})
    volume_id = descriptor.get("volume_id")
    phase_label = descriptor.get("phase_label")
    volumes = data.get("volumes") or []
    phases = data.get("phases") or []

    volume = next((item for item in reversed(volumes) if item.get("id") == volume_id), None)
    volume_relation = "current"
    if volume is None and volumes:
        volume = volumes[-1]
        volume_relation = "previous"
    phase = next(
        (
            item
            for item in reversed(phases)
            if item.get("volume_id") == volume_id and item.get("label") == phase_label
        ),
        None,
    )
    phase_relation = "current"
    if phase is None and volume is not None:
        phase_ids = set(str(item) for item in volume.get("phase_ids") or [])
        phase = next((item for item in reversed(phases) if str(item.get("id")) in phase_ids), None)
        phase_relation = "previous"

    seed = _book_seed(plan)
    out: dict[str, Any] = {
        "schema": MEMORY_SCHEMA,
        "updated_chapter": int(data.get("updated_chapter") or 0),
        "book_spine": {
            "chapter_count": int((data.get("book") or {}).get("chapter_count") or 0),
            "summary": seed,
            "volume_index": [{"id": item.get("id"), "start_chapter": item.get("start_chapter"),
                              "end_chapter": item.get("end_chapter")} for item in volumes],
            "source_path": str(store.hierarchical_memory_path),
        },
    }
    if volume is not None:
        out["volume_summary"] = {
            "id": volume.get("id"),
            "relation": volume_relation,
            "start_chapter": volume.get("start_chapter"),
            "end_chapter": volume.get("end_chapter"),
            "chapter_count": volume.get("chapter_count"),
            "summary": _phase_summary(phase) if phase is not None else "",
            "phase_index": [{"id": item.get("id"), "label": item.get("label"),
                             "start_chapter": item.get("start_chapter"), "end_chapter": item.get("end_chapter")}
                            for item in phases if item.get("volume_id") == volume.get("id")],
            "loaded_phase_ids": [phase.get("id")] if phase is not None else [],
            "source_path": str(store.hierarchical_memory_path),
        }
    if phase is not None:
        out["phase_summary"] = {
            "id": phase.get("id"),
            "label": phase.get("label"),
            "relation": phase_relation,
            "start_chapter": phase.get("start_chapter"),
            "end_chapter": phase.get("end_chapter"),
            "chapter_count": phase.get("chapter_count"),
            "summary": _phase_summary(phase),
            "chapter_sources": [{"chapter": item.get("chapter"), "path": str(store.summary_path(int(item["chapter"]))) }
                                for item in phase.get("chapters") or []],
        }
    return out
