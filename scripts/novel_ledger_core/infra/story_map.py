from __future__ import annotations

from typing import Any
from .util import LedgerError
from .volume_outline import volume_outline_errors


EVENT_SPINE_SCHEMA = "novel-ledger.event-spine.v1"
PLAN_SCHEMA = "novel-ledger.plan.v2"
MAX_PHASE_CHAPTERS = 20
THREAD_KINDS = ("main", "subplot")
CLOCK_KINDS = ("protagonist", "antagonist", "world")
TENSION_MODES = ("build", "reversal", "payoff", "breather", "climax")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _stage_index(stage_names: list[str], value: Any) -> int | None:
    stage = _text(value)
    try:
        return stage_names.index(stage)
    except ValueError:
        return None


def story_map_errors(
    outline: Any,
    *,
    chapters: list[dict[str, Any]] | None = None,
    phases: list[dict[str, Any]] | None = None,
    required: bool = False,
) -> list[dict[str, Any]]:
    """Validate the bounded book-level story map and optional per-chapter references.

    ``required`` is true for hatch v3 and plan.v2 projects, so removing the
    spine cannot silently downgrade the project out of its signed contract.
    """
    if not isinstance(outline, dict):
        return [_error("event_spine_missing", "book_outline is required for a plan.v2 project")] if required else []
    spine = outline.get("event_spine")
    if not isinstance(spine, dict) or spine.get("schema") != EVENT_SPINE_SCHEMA:
        return [
            _error(
                "event_spine_missing",
                f"book_outline.event_spine must declare schema {EVENT_SPINE_SCHEMA}",
            )
        ] if required else []

    errors: list[dict[str, Any]] = []
    acts = outline.get("acts")
    stage_names = [
        _text(item.get("name"))
        for item in (acts if isinstance(acts, list) else [])
        if isinstance(item, dict) and _text(item.get("name"))
    ]
    if not 2 <= len(stage_names) <= 5 or len(set(stage_names)) != len(stage_names):
        errors.append(_error(
            "story_map_invalid_stages",
            "story map requires 2-5 uniquely named acts",
            stages=stage_names,
        ))

    mainline = spine.get("mainline")
    subplots = spine.get("subplots")
    threads: Any = []
    if isinstance(mainline, dict) and isinstance(subplots, list):
        threads = [{**mainline, "kind": "main"}] + [
            {**item, "kind": "subplot"} if isinstance(item, dict) else item
            for item in subplots
        ]
    thread_ids: list[str] = []
    main_ids: list[str] = []
    thread_bounds: dict[str, tuple[int, int]] = {}
    event_index: dict[str, dict[str, str]] = {}
    if not isinstance(threads, list) or not 2 <= len(threads) <= 7:
        errors.append(_error(
            "story_map_thread_count",
            "event_spine must contain one mainline and 1-6 subplots",
        ))
        threads = []
    for index, thread in enumerate(threads):
        field = "event_spine.mainline" if index == 0 else f"event_spine.subplots[{index - 1}]"
        if not isinstance(thread, dict):
            errors.append(_error("story_map_invalid_thread", f"{field} must be an object"))
            continue
        ident = _text(thread.get("id"))
        kind = _text(thread.get("kind"))
        if not ident:
            errors.append(_error("story_map_invalid_thread", f"{field}.id is required"))
        elif ident in thread_ids:
            errors.append(_error("story_map_duplicate_id", f"duplicate story thread id: {ident}", id=ident))
        else:
            thread_ids.append(ident)
        if kind not in THREAD_KINDS:
            errors.append(_error("story_map_invalid_thread", f"{field}.kind must be main or subplot"))
        elif kind == "main" and ident:
            main_ids.append(ident)
        for key in ("name", "purpose", "mainline_link", "open_stage", "payoff_stage"):
            if not _text(thread.get(key)):
                errors.append(_error("story_map_invalid_thread", f"{field}.{key} is required"))
        open_index = _stage_index(stage_names, thread.get("open_stage"))
        payoff_index = _stage_index(stage_names, thread.get("payoff_stage"))
        if open_index is None or payoff_index is None or open_index > payoff_index:
            errors.append(_error(
                "story_map_invalid_stage",
                f"{field} open/payoff stages must exist and be in causal order",
            ))
        elif ident:
            thread_bounds[ident] = (open_index, payoff_index)
        participants = thread.get("participants")
        if not isinstance(participants, list) or not 1 <= len(participants) <= 8 or any(
            not _text(item) for item in participants
        ):
            errors.append(_error(
                "story_map_invalid_thread",
                f"{field}.participants must contain 1-8 non-empty names",
            ))
        touchpoints = thread.get("events")
        if not isinstance(touchpoints, list) or not 2 <= len(touchpoints) <= 8:
            errors.append(_error(
                "story_map_touchpoints",
                f"{field}.events must contain 2-8 bounded event touchpoints",
            ))
            continue
        touchpoint_stages: list[int] = []
        for t_index, touchpoint in enumerate(touchpoints):
            t_field = f"{field}.events[{t_index}]"
            if not isinstance(touchpoint, dict):
                errors.append(_error("story_map_touchpoints", f"{t_field} must be an object"))
                continue
            event_id = _text(touchpoint.get("id"))
            if not event_id:
                errors.append(_error("story_map_missing_event_id", f"{t_field}.id is required"))
            elif event_id in event_index:
                errors.append(_error("story_map_duplicate_event_id", f"duplicate event id: {event_id}", id=event_id))
            else:
                event_index[event_id] = {
                    "layer": "mainline" if kind == "main" else "subplot",
                    "owner_id": ident,
                    "stage": _text(touchpoint.get("stage")),
                    "position": t_index + 1,
                }
            stage_index = _stage_index(stage_names, touchpoint.get("stage"))
            if stage_index is None:
                errors.append(_error("story_map_invalid_stage", f"{t_field}.stage must name an act"))
            else:
                touchpoint_stages.append(stage_index)
            for key in ("event", "change"):
                if not _text(touchpoint.get(key)):
                    errors.append(_error("story_map_touchpoints", f"{t_field}.{key} is required"))
        if touchpoint_stages != sorted(touchpoint_stages):
            errors.append(_error(
                "story_map_touchpoints",
                f"{field}.events must follow act order",
            ))
        if (
            touchpoint_stages
            and open_index is not None
            and payoff_index is not None
            and (touchpoint_stages[0] != open_index or touchpoint_stages[-1] != payoff_index)
        ):
            errors.append(_error(
                "story_map_touchpoint_bounds",
                f"{field}.events must start at open_stage and end at payoff_stage",
            ))
    if len(main_ids) != 1:
        errors.append(_error(
            "story_map_main_count",
            "event_spine must contain exactly one mainline",
            main_ids=main_ids,
        ))

    clocks = spine.get("timelines")
    clock_ids: list[str] = []
    clock_kinds: list[str] = []
    if not isinstance(clocks, list) or len(clocks) != 3:
        errors.append(_error(
            "story_map_clock_count",
            "event_spine.timelines must contain protagonist, antagonist, and world clocks",
        ))
        clocks = []
    for index, clock in enumerate(clocks):
        field = f"event_spine.timelines[{index}]"
        if not isinstance(clock, dict):
            errors.append(_error("story_map_invalid_clock", f"{field} must be an object"))
            continue
        ident = _text(clock.get("id"))
        kind = _text(clock.get("kind"))
        if not ident:
            errors.append(_error("story_map_invalid_clock", f"{field}.id is required"))
        elif ident in clock_ids:
            errors.append(_error("story_map_duplicate_id", f"duplicate timeline clock id: {ident}", id=ident))
        else:
            clock_ids.append(ident)
        if kind not in CLOCK_KINDS:
            errors.append(_error("story_map_invalid_clock", f"{field}.kind is invalid"))
        else:
            clock_kinds.append(kind)
        for key in ("name", "start_state"):
            if not _text(clock.get(key)):
                errors.append(_error("story_map_invalid_clock", f"{field}.{key} is required"))
        events = clock.get("events")
        if not isinstance(events, list) or not 2 <= len(events) <= 8:
            errors.append(_error(
                "story_map_clock_events",
                f"{field}.events must contain 2-8 events",
            ))
            continue
        orders: list[int] = []
        event_stages: list[int] = []
        for e_index, event in enumerate(events):
            e_field = f"{field}.events[{e_index}]"
            if not isinstance(event, dict):
                errors.append(_error("story_map_clock_events", f"{e_field} must be an object"))
                continue
            event_id = _text(event.get("id"))
            if not event_id:
                errors.append(_error("story_map_missing_event_id", f"{e_field}.id is required"))
            elif event_id in event_index:
                errors.append(_error("story_map_duplicate_event_id", f"duplicate event id: {event_id}", id=event_id))
            else:
                event_index[event_id] = {
                    "layer": "timeline",
                    "owner_id": ident,
                    "stage": _text(event.get("stage")),
                    "position": e_index + 1,
                }
            order = event.get("order")
            if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
                errors.append(_error("story_map_clock_events", f"{e_field}.order must be a positive integer"))
            else:
                orders.append(order)
            stage_index = _stage_index(stage_names, event.get("stage"))
            if stage_index is None:
                errors.append(_error("story_map_invalid_stage", f"{e_field}.stage must name an act"))
            else:
                event_stages.append(stage_index)
            for key in ("event", "deadline", "consequence"):
                if not _text(event.get(key)):
                    errors.append(_error("story_map_clock_events", f"{e_field}.{key} is required"))
        if orders != list(range(1, len(events) + 1)):
            errors.append(_error(
                "story_map_clock_events",
                f"{field}.events orders must be consecutive from 1",
            ))
        if event_stages != sorted(event_stages):
            errors.append(_error(
                "story_map_clock_events",
                f"{field}.events must follow act order",
            ))
    if sorted(clock_kinds) != sorted(CLOCK_KINDS):
        errors.append(_error(
            "story_map_clock_kinds",
            "event_spine.timelines must contain each required kind exactly once",
            kinds=clock_kinds,
        ))

    curve = spine.get("tension_curve")
    if not isinstance(curve, list) or len(curve) != len(stage_names):
        errors.append(_error(
            "story_map_tension_count",
            "event_spine.tension_curve must contain exactly one entry per act",
        ))
        curve = []
    curve_stages: list[str] = []
    curve_levels: list[int] = []
    curve_modes: list[str] = []
    for index, point in enumerate(curve):
        field = f"tension_curve[{index}]"
        if not isinstance(point, dict):
            errors.append(_error("story_map_invalid_tension", f"{field} must be an object"))
            continue
        stage = _text(point.get("stage"))
        curve_stages.append(stage)
        level = point.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
            errors.append(_error("story_map_invalid_tension", f"{field}.level must be 1-5"))
        else:
            curve_levels.append(level)
        mode = _text(point.get("mode"))
        if mode not in TENSION_MODES:
            errors.append(_error("story_map_invalid_tension", f"{field}.mode is invalid"))
        else:
            curve_modes.append(mode)
        for key in ("pressure", "turn", "payoff", "next_imbalance"):
            if not _text(point.get(key)):
                errors.append(_error("story_map_invalid_tension", f"{field}.{key} is required"))
    if curve_stages != stage_names:
        errors.append(_error(
            "story_map_tension_stages",
            "tension_curve stages must match acts in order",
            expected=stage_names,
            actual=curve_stages,
        ))
    if len(curve_levels) == len(stage_names) and len(stage_names) > 1:
        if len(set(curve_levels)) < 2 or max(curve_levels) < 4:
            errors.append(_error(
                "story_map_flat_tension",
                "tension_curve must vary across acts and reach level 4 or 5",
            ))
    if len(curve_modes) == len(stage_names) and not ({"payoff", "climax"} & set(curve_modes)):
        errors.append(_error(
            "story_map_missing_payoff",
            "tension_curve must include at least one payoff or climax act",
        ))

    phase_index: dict[str, dict[str, Any]] = {}
    phase_ranges: list[tuple[int, int, str]] = []
    phase_records: list[dict[str, Any]] = []
    phase_required_owners: dict[str, dict[str, set[str]]] = {}
    if phases is not None:
        if not isinstance(phases, list) or not phases:
            errors.append(_error(
                "event_spine_missing_phases",
                "event-spine plans require at least one signed phase brief",
            ))
            phases = []
        for index, phase in enumerate(phases):
            field = f"phases[{index}]"
            if not isinstance(phase, dict):
                errors.append(_error("event_spine_invalid_phase", f"{field} must be an object"))
                continue
            phase_id = _text(phase.get("id"))
            stage = _text(phase.get("story_stage"))
            if not phase_id:
                errors.append(_error("event_spine_invalid_phase", f"{field}.id is required"))
            elif phase_id in phase_index:
                errors.append(_error("event_spine_duplicate_phase", f"duplicate phase id: {phase_id}", id=phase_id))
            else:
                phase_index[phase_id] = phase
            for key in ("name", "objective", "climax"):
                if not _text(phase.get(key)):
                    errors.append(_error(
                        "event_spine_invalid_phase",
                        f"{field}.{key} is required",
                        phase_index=index,
                        phase_id=phase_id or None,
                        missing_field=key,
                    ))
            start = phase.get("chapter_start")
            end = phase.get("chapter_end")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start <= 0
                or end < start
            ):
                errors.append(_error(
                    "event_spine_invalid_phase_range",
                    f"{field} chapter_start/chapter_end must define a positive ordered range",
                ))
            elif phase_id:
                phase_ranges.append((start, end, phase_id))
                if end - start + 1 > MAX_PHASE_CHAPTERS:
                    errors.append(_error(
                        "event_spine_phase_too_long",
                        f"{field} may contain at most {MAX_PHASE_CHAPTERS} chapters",
                        phase_id=phase_id,
                        chapter_count=end - start + 1,
                    ))
            if stage not in stage_names or _text(phase.get("tension_stage")) != stage:
                errors.append(_error(
                    "event_spine_phase_tension_mismatch",
                    f"{field}.story_stage and tension_stage must name the same act",
                ))

            all_phase_refs: list[str] = []
            owner_groups: dict[str, set[str]] = {"thread": set(), "clock": set()}
            ref_groups = (
                ("mainline_event_ref", "mainline", 1, 1),
                ("subplot_event_refs", "subplot", 1, 3),
                ("timeline_event_refs", "timeline", 1, 3),
            )
            for key, layer, minimum, maximum in ref_groups:
                raw_refs = phase.get(key)
                refs = [_text(raw_refs)] if key == "mainline_event_ref" else (
                    [_text(item) for item in raw_refs] if isinstance(raw_refs, list) else []
                )
                if not minimum <= len(refs) <= maximum or any(not item for item in refs):
                    errors.append(_error(
                        "event_spine_phase_refs",
                        f"{field}.{key} must contain {minimum}-{maximum} event reference(s)",
                    ))
                    continue
                if len(set(refs)) != len(refs):
                    errors.append(_error("event_spine_phase_refs", f"{field}.{key} contains duplicate refs"))
                owners: list[str] = []
                for ref in refs:
                    meta = event_index.get(ref)
                    if meta is None or meta["layer"] != layer:
                        errors.append(_error(
                            "event_spine_phase_refs",
                            f"{field}.{key} references an unknown or wrong-layer event: {ref}",
                        ))
                    elif meta["stage"] != stage:
                        errors.append(_error(
                            "event_spine_phase_stage_mismatch",
                            f"{field}.{key} event {ref} does not belong to story_stage {stage}",
                        ))
                    else:
                        all_phase_refs.append(ref)
                        owners.append(meta["owner_id"])
                        owner_groups["clock" if layer == "timeline" else "thread"].add(meta["owner_id"])
                if len(owners) != len(set(owners)):
                    errors.append(_error(
                        "event_spine_phase_duplicate_owner",
                        f"{field}.{key} must not select multiple events from the same owner",
                    ))
            for key in ("entry_state", "exit_state", "tension_change"):
                if not _text(phase.get(key)):
                    errors.append(_error(
                        "event_spine_invalid_phase",
                        f"{field}.{key} is required",
                        phase_index=index,
                        phase_id=phase_id or None,
                        missing_field=key,
                    ))
            event_changes = phase.get("event_changes")
            if not isinstance(event_changes, dict):
                errors.append(_error(
                    "event_spine_phase_event_changes",
                    f"{field}.event_changes must map every selected event id to a concrete state change",
                ))
            else:
                expected_refs = set(all_phase_refs)
                actual_refs = {_text(key) for key in event_changes}
                if actual_refs != expected_refs or any(not _text(value) for value in event_changes.values()):
                    errors.append(_error(
                        "event_spine_phase_event_changes",
                        f"{field}.event_changes must contain exactly the selected event ids with non-empty changes",
                        expected=sorted(expected_refs),
                        actual=sorted(actual_refs),
                    ))
            if phase_id:
                phase_required_owners[phase_id] = owner_groups
                phase_records.append({
                    "id": phase_id,
                    "start": start,
                    "end": end,
                    "stage_index": _stage_index(stage_names, stage),
                    "refs": list(all_phase_refs),
                })
        for previous, current in zip(sorted(phase_ranges), sorted(phase_ranges)[1:]):
            if current[0] <= previous[1]:
                errors.append(_error(
                    "event_spine_phase_overlap",
                    f"phase chapter ranges overlap: {previous[2]} and {current[2]}",
                ))
            elif current[0] != previous[1] + 1:
                errors.append(_error(
                    "event_spine_phase_gap",
                    f"phase chapter ranges must be contiguous: {previous[2]} and {current[2]}",
                ))
        last_event_position: dict[str, int] = {}
        previous_stage_index: int | None = None
        for record in sorted(
            phase_records,
            key=lambda item: item["start"] if isinstance(item.get("start"), int) else 10**12,
        ):
            stage_index = record.get("stage_index")
            if isinstance(stage_index, int):
                if previous_stage_index is not None and stage_index < previous_stage_index:
                    errors.append(_error(
                        "event_spine_phase_stage_regression",
                        f"phase {record['id']} moves backward to an earlier story stage",
                    ))
                previous_stage_index = stage_index
            for ref in record.get("refs") or []:
                meta = event_index.get(ref) or {}
                owner = _text(meta.get("owner_id"))
                position = meta.get("position")
                if not owner or not isinstance(position, int):
                    continue
                previous_position = last_event_position.get(owner)
                if previous_position is not None and position < previous_position:
                    errors.append(_error(
                        "event_spine_event_regression",
                        f"phase {record['id']} moves {owner} backward from event {previous_position} to {position}",
                    ))
                last_event_position[owner] = position

    if chapters is not None:
        valid_threads = set(thread_ids)
        valid_clocks = set(clock_ids)
        phase_usage: dict[str, dict[str, Any]] = {}
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            number = chapter.get("chapter")
            phase_id = _text(chapter.get("phase_id"))
            phase = phase_index.get(phase_id) if phases is not None else None
            if phases is not None and phase is None:
                errors.append(_error(
                    "event_spine_unknown_phase",
                    f"chapter {number} must reference a signed phase_id",
                    chapter=number,
                    phase_id=phase_id,
                ))
            story_stage = _text(chapter.get("story_stage"))
            chapter_stage_index = _stage_index(stage_names, story_stage)
            if chapter_stage_index is None:
                errors.append(_error(
                    "story_map_invalid_chapter_stage",
                    f"chapter {number} story_stage must name an act",
                    chapter=number,
                    stage=story_stage,
                ))
            thread_refs = chapter.get("thread_refs")
            thread_normalized: list[str] = []
            if not isinstance(thread_refs, list) or not thread_refs or any(not _text(item) for item in thread_refs):
                errors.append(_error(
                    "story_map_missing_thread_refs",
                    f"chapter {number}: the field is named `thread_refs` (a non-empty list of 1-3 story-thread ids, "
                    "e.g. [\"thread-main\"]) — other spellings like `threads` are not read",
                    chapter=number,
                ))
            else:
                thread_normalized = [_text(item) for item in thread_refs]
                if len(thread_normalized) > 3:
                    errors.append(_error(
                        "story_map_thread_refs_over_cap",
                        f"chapter {number} may reference at most 3 story threads",
                        chapter=number,
                        count=len(thread_normalized),
                    ))
                unknown = sorted(set(thread_normalized) - valid_threads)
                if unknown:
                    errors.append(_error(
                        "story_map_unknown_thread_ref",
                        f"chapter {number} references unknown story threads",
                        chapter=number,
                        refs=unknown,
                    ))
                duplicates = sorted({item for item in thread_normalized if thread_normalized.count(item) > 1})
                if duplicates:
                    errors.append(_error(
                        "story_map_duplicate_thread_ref",
                        f"chapter {number} repeats story thread references",
                        chapter=number,
                        refs=duplicates,
                    ))
            clock_refs = chapter.get("clock_refs")
            if phase is not None:
                start = phase.get("chapter_start")
                end = phase.get("chapter_end")
                if not (isinstance(number, int) and isinstance(start, int) and isinstance(end, int) and start <= number <= end):
                    errors.append(_error(
                        "event_spine_chapter_outside_phase",
                        f"chapter {number} falls outside phase {phase_id} range",
                        chapter=number,
                        phase_id=phase_id,
                    ))
                if story_stage != _text(phase.get("story_stage")):
                    errors.append(_error(
                        "event_spine_chapter_stage_mismatch",
                        f"chapter {number} story_stage must match phase {phase_id}",
                        chapter=number,
                        phase_id=phase_id,
                    ))

                required_threads = {
                    event_index[ref]["owner_id"]
                    for ref in [phase.get("mainline_event_ref"), *(phase.get("subplot_event_refs") or [])]
                    if ref in event_index
                }
                required_clocks = {
                    event_index[ref]["owner_id"]
                    for ref in (phase.get("timeline_event_refs") or [])
                    if ref in event_index
                }
                actual_threads = {_text(item) for item in (thread_refs or [])}
                actual_clocks = {_text(item) for item in (clock_refs or [])}
                usage = phase_usage.setdefault(phase_id, {
                    "threads": set(), "clocks": set(), "chapters": set(), "volumes": set(),
                })
                usage["threads"].update(actual_threads)
                usage["clocks"].update(actual_clocks)
                if isinstance(number, int):
                    usage["chapters"].add(number)
                usage["volumes"].add(_text(chapter.get("volume") if chapter.get("volume") is not None else 1))
                if not actual_threads <= required_threads:
                    errors.append(_error(
                        "event_spine_chapter_phase_thread_mismatch",
                        f"chapter {number} references threads outside phase {phase_id}",
                        refs=sorted(actual_threads - required_threads),
                    ))
                if not actual_clocks <= required_clocks:
                    errors.append(_error(
                        "event_spine_chapter_phase_clock_mismatch",
                        f"chapter {number} references clocks outside phase {phase_id}",
                        refs=sorted(actual_clocks - required_clocks),
                    ))
                if chapter_stage_index is not None:
                    inactive = sorted({
                        ident
                        for ident in thread_normalized
                        if ident in thread_bounds
                        and not thread_bounds[ident][0] <= chapter_stage_index <= thread_bounds[ident][1]
                    })
                    if inactive:
                        errors.append(_error(
                            "story_map_inactive_thread_ref",
                            f"chapter {number} references story threads outside their active act range",
                            chapter=number,
                            refs=inactive,
                        ))
            if not isinstance(clock_refs, list) or not clock_refs or any(not _text(item) for item in clock_refs):
                errors.append(_error(
                    "story_map_missing_clock_refs",
                    f"chapter {number}: the field is named `clock_refs` (a non-empty list of 1-2 timeline-clock ids, "
                    "e.g. [\"clock-protagonist\"]) — other spellings like `clocks` are not read",
                    chapter=number,
                ))
            else:
                clock_normalized = [_text(item) for item in clock_refs]
                if len(clock_normalized) > 2:
                    errors.append(_error(
                        "story_map_clock_refs_over_cap",
                        f"chapter {number} may reference at most 2 timeline clocks",
                        chapter=number,
                        count=len(clock_normalized),
                    ))
                unknown = sorted(set(clock_normalized) - valid_clocks)
                if unknown:
                    errors.append(_error(
                        "story_map_unknown_clock_ref",
                        f"chapter {number} references unknown timeline clocks",
                        chapter=number,
                        refs=unknown,
                    ))
                duplicates = sorted({item for item in clock_normalized if clock_normalized.count(item) > 1})
                if duplicates:
                    errors.append(_error(
                        "story_map_duplicate_clock_ref",
                        f"chapter {number} repeats timeline clock references",
                        chapter=number,
                        refs=duplicates,
                    ))
        for phase_id, phase in phase_index.items():
            usage = phase_usage.get(phase_id) or {
                "threads": set(), "clocks": set(), "chapters": set(), "volumes": set(),
            }
            required_owners = phase_required_owners.get(phase_id) or {"thread": set(), "clock": set()}
            missing_threads = sorted(required_owners["thread"] - usage["threads"])
            missing_clocks = sorted(required_owners["clock"] - usage["clocks"])
            if missing_threads or missing_clocks:
                errors.append(_error(
                    "event_spine_phase_coverage",
                    f"phase {phase_id} chapters must collectively cover every selected story line and timeline",
                    missing_threads=missing_threads,
                    missing_clocks=missing_clocks,
                ))
            start = phase.get("chapter_start")
            end = phase.get("chapter_end")
            if isinstance(start, int) and isinstance(end, int):
                expected_chapters = set(range(start, end + 1))
                if usage["chapters"] != expected_chapters:
                    errors.append(_error(
                        "event_spine_phase_chapter_coverage",
                        f"phase {phase_id} must contain every chapter in its signed range",
                        missing=sorted(expected_chapters - usage["chapters"]),
                        extra=sorted(usage["chapters"] - expected_chapters),
                    ))
            if len(usage["volumes"]) > 1:
                errors.append(_error(
                    "event_spine_phase_crosses_volume",
                    f"phase {phase_id} must stay within one volume",
                    volumes=sorted(usage["volumes"]),
                ))

    return errors


def _beat_evidence(chapter: dict[str, Any]) -> tuple[str, set[str]]:
    """本章拍点正文与在场人物——判断某条故事线是否有本章场次支撑的证据面。"""
    beat_text = "".join(
        _text(beat.get("text"))
        for beat in (chapter.get("beats") or [])
        if isinstance(beat, dict)
    )
    present = {_text(name) for name in (chapter.get("present") or []) if _text(name)}
    return beat_text, present


def _thread_has_beat_support(
    participants: list[str],
    kind: str,
    *,
    protagonist: str,
    beat_text: str,
    present: set[str],
) -> bool:
    """本章拍点是否真的给这条线留了场次。

    现场曾整卷出现：`thread_refs` 与自己的 beats 对不上——挂了某条线而本场戏里
    没有该线的具名参与者。而 pack 会把 story_focus 渲染成"本章推进故事线 X：作用是……"的**写作指令**，
    执笔编辑照做就会给一个没有场次支撑的人物硬塞戏。当时靠总编辑在每章派发简报里手写
    禁令挡住，属于一章一次的人力补丁。

    判定口径（保守，只标"明显没支撑"，绝不误伤）：
    - 主线（kind=main）永远算有支撑——主角本人就是主线的执行者；
    - 拿掉主角后该线没有别的具名参与者 → 无法判定，算有支撑（不误报）；
    - 其余：该线任一非主角参与者出现在本章拍点正文或在场名单里，即算有支撑。
    """
    if kind == "main":
        return True
    others = [name for name in participants if name and name != protagonist]
    if not others:
        return True
    return any(name in beat_text or name in present for name in others)


def story_focus(
    outline: Any,
    chapter: Any,
    phases: Any = None,
    *,
    protagonist: str = "",
) -> dict[str, Any]:
    """Project only the current chapter's signed story-map slice into its pack.

    `protagonist` 用于剔除"主角同名"造成的假支撑（见 `_thread_has_beat_support`）；
    缺省空串时退化为"只看参与者是否出现"，仍是保守方向（宁可少报不可误报）。
    """
    if (
        not isinstance(outline, dict)
        or not isinstance(chapter, dict)
    ):
        return {}
    spine = outline.get("event_spine")
    if not isinstance(spine, dict) or spine.get("schema") != EVENT_SPINE_SCHEMA:
        return {}

    stage = _text(chapter.get("story_stage"))
    thread_refs = {_text(item) for item in (chapter.get("thread_refs") or []) if _text(item)}
    clock_refs = {_text(item) for item in (chapter.get("clock_refs") or []) if _text(item)}
    focus: dict[str, Any] = {"stage": stage}
    phase_id = _text(chapter.get("phase_id"))
    phase = next(
        (
            item
            for item in (phases if isinstance(phases, list) else [])
            if isinstance(item, dict) and _text(item.get("id")) == phase_id
        ),
        None,
    )
    if phase is not None:
        focus["phase"] = {
            key: phase.get(key)
            for key in (
                "id", "name", "objective", "climax", "mainline_event_ref",
                "subplot_event_refs", "timeline_event_refs", "tension_stage",
                "chapter_start", "chapter_end", "entry_state", "exit_state",
                "event_changes", "tension_change",
            )
        }
        focus["phase"]["is_phase_end"] = chapter.get("chapter") == phase.get("chapter_end")

    selected_thread_events = set()
    selected_timeline_events = set()
    if isinstance(phase, dict):
        selected_thread_events = {
            _text(phase.get("mainline_event_ref")),
            *(_text(item) for item in (phase.get("subplot_event_refs") or [])),
        }
        selected_timeline_events = {_text(item) for item in (phase.get("timeline_event_refs") or [])}

    selected_threads: list[dict[str, Any]] = []
    raw_threads: list[dict[str, Any]] = []
    if isinstance(spine.get("mainline"), dict):
        raw_threads.append({**spine["mainline"], "kind": "main"})
    raw_threads.extend(
        {**item, "kind": "subplot"}
        for item in (spine.get("subplots") or [])
        if isinstance(item, dict)
    )
    beat_text, present_names = _beat_evidence(chapter)
    for raw in raw_threads:
        if not isinstance(raw, dict) or _text(raw.get("id")) not in thread_refs:
            continue
        points = [
            {
                "event": _text(point.get("event")),
                "change": _text(point.get("change")),
            }
            for point in (raw.get("events") or [])
            if isinstance(point, dict)
            and _text(point.get("stage")) == stage
            and (not selected_thread_events or _text(point.get("id")) in selected_thread_events)
        ]
        thread_kind = _text(raw.get("kind"))
        participants = [_text(name) for name in (raw.get("participants") or []) if _text(name)]
        supported = _thread_has_beat_support(
            participants,
            thread_kind,
            protagonist=protagonist,
            beat_text=beat_text,
            present=present_names,
        )
        entry = {
            "id": _text(raw.get("id")),
            "kind": thread_kind,
            "name": _text(raw.get("name")),
            "purpose": _text(raw.get("purpose")),
            "mainline_link": _text(raw.get("mainline_link")),
            "stage_touchpoints": points,
            "beats_support": supported,
        }
        if not supported:
            # 只标事实，不判死刑：视图与拍点冲突时一律以 beats 为准（见 roles/派发模板）。
            entry["beats_support_note"] = (
                "本章拍点与在场名单里都没有该线的具名参与者：只可在既有场次内自然带出，"
                "不得为它另起场次或硬塞人物；若确需该线出场，先回策划改章拍"
            )
        selected_threads.append(entry)
    if selected_threads:
        focus["threads"] = selected_threads

    selected_clocks: list[dict[str, Any]] = []
    for raw in spine.get("timelines") or []:
        if not isinstance(raw, dict) or _text(raw.get("id")) not in clock_refs:
            continue
        events = [
            {
                "event": _text(event.get("event")),
                "deadline": _text(event.get("deadline")),
                "consequence": _text(event.get("consequence")),
            }
            for event in (raw.get("events") or [])
            if isinstance(event, dict)
            and _text(event.get("stage")) == stage
            and (not selected_timeline_events or _text(event.get("id")) in selected_timeline_events)
        ]
        selected_clocks.append({
            "id": _text(raw.get("id")),
            "kind": _text(raw.get("kind")),
            "name": _text(raw.get("name")),
            "start_state": _text(raw.get("start_state")),
            "stage_events": events,
        })
    if selected_clocks:
        focus["clocks"] = selected_clocks

    point = next(
        (
            raw
            for raw in (spine.get("tension_curve") or [])
            if isinstance(raw, dict) and _text(raw.get("stage")) == stage
        ),
        None,
    )
    if point is not None:
        focus["tension"] = {
            key: point.get(key)
            for key in ("level", "mode", "pressure", "turn", "payoff", "next_imbalance")
        }
    return focus


def plan_story_map_errors(
    plan: dict[str, Any],
    *,
    required: bool | None = None,
    volume_outline_required: bool = False,
) -> list[dict[str, Any]]:
    raw_chapters = plan.get("chapters")
    raw_phases = plan.get("phases")
    contract_required = plan.get("schema") == PLAN_SCHEMA or bool(required)
    errors: list[dict[str, Any]] = []
    if contract_required and plan.get("schema") != PLAN_SCHEMA:
        errors.append(_error(
            "event_spine_plan_schema_missing",
            f"hatch projects must retain plan schema {PLAN_SCHEMA}",
        ))
    errors.extend(story_map_errors(
        plan.get("book_outline"),
        chapters=raw_chapters if isinstance(raw_chapters, list) else [],
        phases=raw_phases if isinstance(raw_phases, list) else [],
        required=contract_required,
    ))
    errors.extend(volume_outline_errors(plan, required=volume_outline_required))
    return errors


# 组装匹配器（content/gates.py _find_actual）逐 kind 依赖的稳定键；与匹配器
# 分支一一对应，改匹配器必须同步这张表。infra 层不得 import content，故镜像于此。
# items 不在此表但必须单独校验：匹配器以 id（双方都写时）或 name（计划未写 id 时）
# 认兑现——只写 `item` 之类键的条目组装端永远认不出（expected_delta_missing → blocked）。
# deaths/new_names 键形宽松（字符串或 name/who），不列硬键。
_EFFECTS_STABLE_KEYS: dict[str, tuple[str, ...]] = {
    "moves": ("who", "to"),
    "facts": ("who", "text"),
    "debts": ("id",),
    "hooks": ("id",),
    "relations": ("who", "target", "kind"),
    "knowledge": ("who", "topic_id"),
    "conditions": ("who", "text"),
}


def must_word_issues(chapters: list[Any]) -> list[dict[str, Any]]:
    """must 锚词不在本拍文本中的清单（机械可判定）。

    hatch 初签早已硬性要求 must 是拍文本子串；扩纲落盘口缺同类防线时，写漂的
    must 要到 select-batch 封批/写作期才炸（真实批次曾 ×5 人工对齐并三次重封）。
    """
    issues: list[dict[str, Any]] = []
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        for idx, beat in enumerate(ch.get("beats") or []):
            if not isinstance(beat, dict):
                continue
            must = beat.get("must")
            beat_text = beat.get("text")
            # 只管规范形状（text + 字符串 must）；scene/description+must 列表的旧形状
            # 不在本闸门管辖，交由 validate 的软提示兜底。
            if not isinstance(must, str) or not isinstance(beat_text, str):
                continue
            must_word = must.strip()
            if must_word and must_word not in beat_text:
                issues.append({
                    "chapter": ch.get("chapter"),
                    "beat_index": idx,
                    "beat": str(beat.get("id") or ""),
                    "must": must_word,
                })
    return issues


def plan_shape_reject(chapters: list[Any], *, origin: str) -> None:
    """章拍形状的统一拒收口：effects 稳定键 + must∈拍文本 + 同章 knowledge 唯一，extend/init 共用。

    缺键条目（如 facts 用 `fact`、items 用 `item`）在组装提交时无论写成什么
    都无法对账，只能 blocked 停线；must 不在拍文本则是终稿永难兑现的锚词；
    同章同 (who, topic) 的两条 knowledge 则与提交端 `knowledge_duplicate_topic`
    硬禁构成互斥死锁——expected 要求逐条回填、delta 又不许写第二条，任何提交
    都无法通过且计划层无修补通道（实测整章停线、被迫手改真源）。三者都机械
    可判定，所有能把章拍写进真源的入口都必须先过这道。
    """
    effects = plan_effects_key_issues(chapters)
    if effects:
        raise LedgerError(
            "invalid_plan",
            "effects entries missing stable keys (assembly matcher cannot reconcile "
            "them; e.g. facts entries need 'text', not 'fact'; items entries need "
            "'name' or 'id', not 'item')",
            {"issues": effects[:10], "count": len(effects), "origin": origin},
        )
    musts = must_word_issues(chapters)
    if musts:
        raise LedgerError(
            "invalid_plan",
            "must anchor words must be verbatim substrings of their beat text "
            "(hatch starter chapters already enforce this); a drifted must can never "
            "be honored by any draft",
            {"issues": musts[:10], "count": len(musts), "origin": origin},
        )
    dups = plan_knowledge_dup_issues(chapters)
    if dups:
        raise LedgerError(
            "invalid_plan",
            "duplicate knowledge effects within one chapter (same who+topic_id): the "
            "submit gate knowledge_duplicate_topic forbids a second delta entry, while "
            "expected_delta_missing demands every planned entry be filled — an "
            "unsatisfiable plan that deadlocks the chapter. Merge them into one entry.",
            {"issues": dups[:10], "count": len(dups), "origin": origin},
        )


def plan_knowledge_dup_issues(chapters: list[Any]) -> list[dict[str, Any]]:
    """同章跨拍收集重复的 (who, topic_id) knowledge 效果声明。"""
    issues: list[dict[str, Any]] = []
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        seen: dict[tuple[str, str], str] = {}
        for beat in ch.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            effects = beat.get("effects") or {}
            if not isinstance(effects, dict):
                continue
            for entry in effects.get("knowledge") or []:
                if not isinstance(entry, dict):
                    continue
                ident = (str(entry.get("who") or "").strip(), str(entry.get("topic_id") or "").strip())
                if not ident[0] or not ident[1]:
                    continue
                first_beat = seen.get(ident)
                if first_beat:
                    issues.append({
                        "chapter": ch.get("chapter"),
                        "who": ident[0],
                        "topic_id": ident[1],
                        "first_beat": first_beat,
                        "duplicate_beat": str(beat.get("id") or ""),
                        "hint": "merge into one knowledge entry (one claim per who+topic per chapter)",
                    })
                else:
                    seen[ident] = str(beat.get("id") or "")
    return issues


def plan_effects_key_issues(chapters: list[Any]) -> list[dict[str, Any]]:
    """章拍 effects 条目的稳定键缺失清单。

    组装提交按稳定键把 effects 与实际 delta 对账；条目缺键（如 facts 写了
    `fact` 而非 `text`）时，该章无论怎么写都判 expected_delta_missing，只能
    blocked 停线。这道检查让坏形状在 plan extend 落盘前就被点名，而不是
    若干章后锁死组装（真实项目曾因此停机在组装相位）。
    """
    issues: list[dict[str, Any]] = []
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        for beat in ch.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            effects = beat.get("effects") or {}
            if not isinstance(effects, dict):
                continue
            for kind, entries in effects.items():
                if kind in ("revivals", "nonliving"):
                    names: set[str] = set()
                    if not isinstance(entries, list):
                        issues.append({"chapter": ch.get("chapter"), "beat": str(beat.get("id") or ""), "kind": kind, "missing_keys": ["name list"]})
                        continue
                    for index, entry in enumerate(entries):
                        name = entry.get("who") if isinstance(entry, dict) else entry
                        if not isinstance(name, str) or not name.strip() or name.strip() in names:
                            issues.append({"chapter": ch.get("chapter"), "beat": str(beat.get("id") or ""), "kind": kind, "index": index, "missing_keys": ["unique non-empty who"]})
                        else:
                            names.add(name.strip())
                    continue
                required = _EFFECTS_STABLE_KEYS.get(kind)
                # items/locations 不在表内但必须校验（见 _EFFECTS_STABLE_KEYS 注释）：
                # 特判必须先于 required is None 的放行，否则永远不可达。
                if (required is None and kind not in ("items", "locations")) or not isinstance(entries, list):
                    continue
                for index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        continue
                    if kind == "items":
                        missing = (
                            []
                            if (
                                str(entry.get("id") or "").strip()
                                or str(entry.get("name") or "").strip()
                            )
                            else ["id or name"]
                        )
                    elif kind == "locations":
                        # 与 items 同款双键（id 或 name 至少其一）；带 attributes 时必须是非空对象。
                        missing = (
                            []
                            if (
                                str(entry.get("id") or "").strip()
                                or str(entry.get("name") or "").strip()
                            )
                            else ["id or name"]
                        )
                        attrs = entry.get("attributes")
                        if attrs is not None and (not isinstance(attrs, dict) or not attrs):
                            missing.append("attributes (non-empty object when present)")
                    else:
                        missing = [key for key in required if not str(entry.get(key) or "").strip()]
                    if missing:
                        issues.append(
                            {
                                "chapter": ch.get("chapter"),
                                "beat": str(beat.get("id") or ""),
                                "kind": kind,
                                "index": index,
                                "missing_keys": missing,
                                "entry_keys": sorted(str(k) for k in entry),
                            }
                        )
    return issues
