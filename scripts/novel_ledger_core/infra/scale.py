from __future__ import annotations

import math
import re
from typing import Any


# 全书终局只能落在规划末段。这个阈值不是要求正文必须机械写满 85%，
# 而是防止阶段小纲把百万字长篇在几十章时误判为已经进入大结局。
ENDGAME_MIN_RATIO = 0.85
COMPLETION_MIN_RATIO = 0.90
# New books sign the same safe writing aim used by content.pack.word_targets:
# default word_band.min 2500 + 700 buffer = 3200. Older signed plans retain
# their declared pace, so a skill update cannot rewrite an existing book's scale.
CHAPTER_WORD_TARGET = 3200
WORD_BAND_AIM_BUFFER = 700
PLAN_CHAPTER_TOLERANCE = 0.20
TERMINAL_BUDGET_TOLERANCE = 0.10

_BOOK_END_RE = re.compile(
    r"(?:全书|整书|本书).{0,8}(?:大结局|完结|收官|终局)|"
    r"(?:大结局|最终章).{0,8}(?:全书|整书|本书)|"
    r"全书圆满|正文完结"
)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def chapter_word_target_for_band(word_band: Any) -> int:
    """Derive a planning pace from the same bounded writing aim as the chapter pack."""
    band = word_band if isinstance(word_band, dict) and word_band else {"min": 2500, "max": 8000}
    minimum = _positive_int(band.get("min")) or 2500
    maximum = _positive_int(band.get("max")) or minimum
    return min(minimum + WORD_BAND_AIM_BUFFER, maximum) if maximum > minimum else minimum


def _chapter_text(chapter: dict[str, Any]) -> str:
    parts = [
        str(chapter.get("title") or ""),
        str(chapter.get("goal") or ""),
        str(chapter.get("recap") or ""),
    ]
    for beat in chapter.get("beats") or []:
        if isinstance(beat, dict):
            parts.extend((str(beat.get("text") or ""), str(beat.get("must") or "")))
        else:
            parts.append(str(beat))
    return "\n".join(parts)


def terminal_book_chapters(plan: dict[str, Any]) -> list[int]:
    """Return chapters that explicitly claim to end the whole book."""
    found: list[int] = []
    for chapter in plan.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        number = _positive_int(chapter.get("chapter"))
        if number is None:
            continue
        scope = str(chapter.get("ending_scope") or "").strip().lower()
        if scope == "book" or _BOOK_END_RE.search(_chapter_text(chapter)):
            found.append(number)
    return sorted(set(found))


def derive_chapter_word_target(plan: dict[str, Any], config: dict[str, Any] | None = None) -> int | None:
    """Return signed pace, or the configured writing aim for unsigned volume budgets."""
    outline = plan.get("book_outline")
    if isinstance(outline, dict):
        explicit = _positive_int(outline.get("chapter_words_target"))
        if explicit is not None:
            return explicit
    volumes = plan.get("volumes")
    if not isinstance(volumes, dict):
        return None
    for volume in volumes.values():
        if not isinstance(volume, dict):
            continue
        words = _positive_number(volume.get("word_budget"))
        chapters = _positive_number(volume.get("chapters_budget"))
        if words is not None and chapters is not None:
            # Unsigned legacy plans may already have a volume pace (for example
            # 250,000 words / 100 chapters). Preserve that commitment when extending.
            # New unsigned plans have no volume yet and use the configured writing aim.
            inferred = max(1, math.ceil(words / chapters))
            if expected_total_chapters(int(words), inferred) == int(chapters):
                return inferred
            return chapter_word_target_for_band((config or {}).get("word_band"))
    return None


def expected_total_chapters(book_words: int, chapter_words_target: int) -> int:
    return max(1, math.ceil(book_words / chapter_words_target))


def rebudget_contract(outline: dict[str, Any], target_words: int, pace: int) -> tuple[int | None, dict[str, Any] | None]:
    """Validate a bounded continuation budget while retaining the signed word target."""
    basis = outline.get("chapter_rebudget")
    if basis is None:
        return None, None
    if not isinstance(basis, dict) or basis.get("schema") != "novel-ledger.chapter-rebudget.v1":
        return None, {"code": "book_scale_rebudget_invalid", "message": "invalid chapter rebudget basis"}
    signed = _positive_int(basis.get("signed_total_chapters"))
    through = _positive_int(basis.get("through_chapter"))
    written = _positive_int(basis.get("written_words"))
    if (signed != expected_total_chapters(target_words, pace) or through is None or written is None
            or basis.get("book_words") != target_words or basis.get("chapter_words_target") != pace
            or not str(basis.get("continuation_volume") or "").strip()):
        return None, {"code": "book_scale_rebudget_invalid", "message": "rebudget must retain signed words/pace and an actual committed baseline"}
    required = max(signed, through + math.ceil(max(target_words - written, 0) / pace))
    if basis.get("total_chapters") != required or basis.get("additional_chapters") != required - signed:
        return None, {"code": "book_scale_rebudget_invalid", "message": "rebudget must equal committed chapters plus remaining words at the signed pace"}
    return required, None


def scale_contract_errors(
    plan: dict[str, Any],
    config: dict[str, Any],
    *,
    written_words: int = 0,
) -> list[dict[str, Any]]:
    """Validate the book-scale contract independently of prose and ledger state.

    The contract is intentionally active only when ``book_outline`` exists. New books
    created by ``book hatch`` always have it; tiny fixture/manual plans remain outside
    this long-form guard.
    """
    outline = plan.get("book_outline")
    if not isinstance(outline, dict):
        return []

    errors: list[dict[str, Any]] = []
    config_words = _positive_int(config.get("book_words"))
    outline_words = _positive_int(outline.get("book_words"))
    target_words = outline_words or config_words
    if target_words is None:
        errors.append({
            "code": "book_scale_missing_words",
            "message": "book_outline.book_words must be a positive integer",
        })
        return errors
    if config_words is not None and outline_words is not None and config_words != outline_words:
        errors.append({
            "code": "book_scale_target_mismatch",
            "book_words": outline_words,
            "config_book_words": config_words,
            "message": "book_outline.book_words must equal config.book_words",
        })

    chapter_target = derive_chapter_word_target(plan, config)
    total_chapters = _positive_int(outline.get("total_chapters"))
    if "chapter_words_target" in outline and _positive_int(outline.get("chapter_words_target")) is None:
        errors.append({
            "code": "book_scale_chapter_target_invalid",
            "chapter_words_target": outline.get("chapter_words_target"),
            "message": "chapter_words_target must be a positive integer",
        })
    # Only a signed scale (both pace and total chapters) is enforceable. ``book hatch``
    # always writes both; small hand-authored diagnostic plans may omit them.
    if chapter_target is not None and total_chapters is not None:
        revised, rebudget_error = rebudget_contract(outline, target_words, chapter_target)
        if rebudget_error:
            errors.append(rebudget_error)
        expected = revised or expected_total_chapters(target_words, chapter_target)
        if total_chapters != expected:
            errors.append({
                "code": "book_scale_total_chapters_mismatch",
                "book_words": target_words,
                "chapter_words_target": chapter_target,
                "total_chapters": total_chapters,
                "expected_total_chapters": expected,
                "message": (
                    f"{target_words} words at about {chapter_target} words/chapter requires about "
                    f"{expected} chapters; declared total_chapters={total_chapters} collapses the book scale"
                ),
            })

    chapters = [item for item in (plan.get("chapters") or []) if isinstance(item, dict)]
    chapter_numbers = [_positive_int(item.get("chapter")) for item in chapters]
    max_planned = max((number for number in chapter_numbers if number is not None), default=0)
    if total_chapters is not None and max_planned > total_chapters:
        errors.append({
            "code": "book_scale_plan_exceeds_total",
            "max_planned_chapter": max_planned,
            "total_chapters": total_chapters,
            "message": "planned chapter number exceeds book_outline.total_chapters",
        })

    volumes = plan.get("volumes")
    volume_word_budgets: list[int] = []
    volume_chapter_budgets: list[int] = []
    if isinstance(volumes, dict):
        for key, volume in volumes.items():
            if not isinstance(volume, dict):
                continue
            words = _positive_int(volume.get("word_budget"))
            count = _positive_int(volume.get("chapters_budget"))
            if words is not None:
                volume_word_budgets.append(words)
            if count is not None:
                volume_chapter_budgets.append(count)
            if words is not None and count is not None and chapter_target is not None:
                expected_count = expected_total_chapters(words, chapter_target)
                basis = outline.get("chapter_rebudget") or {}
                if isinstance(basis, dict) and str(key) == str(basis.get("continuation_volume")):
                    expected_count += _positive_int(basis.get("additional_chapters")) or 0
                if count != expected_count:
                    errors.append({
                        "code": "book_scale_volume_chapters_mismatch",
                        "volume": str(key),
                        "word_budget": words,
                        "chapters_budget": count,
                        "expected_chapters_budget": expected_count,
                        "chapter_words_target": chapter_target,
                        "message": f"volume chapters_budget must equal ceil(word_budget / {chapter_target})",
                    })

    if total_chapters is not None:
        earliest_end = math.ceil(total_chapters * ENDGAME_MIN_RATIO)
        milestones = outline.get("milestones")
        if isinstance(milestones, list):
            for index, milestone in enumerate(milestones):
                if not isinstance(milestone, dict) or milestone.get("kind") != "book_climax":
                    continue
                chapter = _positive_int(milestone.get("chapter"))
                basis = outline.get("chapter_rebudget") or {}
                already_written = isinstance(basis, dict) and chapter is not None and chapter <= (_positive_int(basis.get("through_chapter")) or 0)
                if chapter is not None and chapter < earliest_end and not already_written:
                    errors.append({
                        "code": "book_scale_early_climax",
                        "milestone_index": index,
                        "chapter": chapter,
                        "earliest_endgame_chapter": earliest_end,
                        "message": "book_climax is scheduled before the final 15% of the signed chapter scale",
                    })

        terminals = terminal_book_chapters(plan)
        progress = written_words / target_words if target_words > 0 else 0.0
        early = [chapter for chapter in terminals if chapter < earliest_end]
        if early and progress < ENDGAME_MIN_RATIO:
            errors.append({
                "code": "book_scale_early_ending",
                "chapters": early,
                "total_chapters": total_chapters,
                "earliest_endgame_chapter": earliest_end,
                "book_words_progress": round(progress, 4),
                "message": "whole-book ending language appears before the signed book scale reaches its endgame",
            })
        if terminals:
            if not volume_word_budgets or abs(sum(volume_word_budgets) - target_words) / target_words > TERMINAL_BUDGET_TOLERANCE:
                errors.append({
                    "code": "book_scale_terminal_volume_words_mismatch",
                    "sum_volume_budgets": sum(volume_word_budgets),
                    "book_words": target_words,
                    "tolerance": TERMINAL_BUDGET_TOLERANCE,
                    "message": "a whole-book ending requires complete volume word budgets close to the book target",
                })
            if not volume_chapter_budgets or abs(sum(volume_chapter_budgets) - total_chapters) / total_chapters > PLAN_CHAPTER_TOLERANCE:
                errors.append({
                    "code": "book_scale_terminal_volume_chapters_mismatch",
                    "sum_volume_chapter_budgets": sum(volume_chapter_budgets),
                    "total_chapters": total_chapters,
                    "tolerance": PLAN_CHAPTER_TOLERANCE,
                    "message": "a whole-book ending requires complete volume chapter budgets close to total_chapters",
                })

    return errors
