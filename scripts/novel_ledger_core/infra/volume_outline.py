"""The signed whole-book volume map; chapter work only refines existing volumes."""
from __future__ import annotations

import copy
import re
from typing import Any

from .scale import expected_total_chapters
from .util import canonical_json, sha256_text


VOLUME_OUTLINE_SCHEMA = "novel-ledger.volume-outlines.v1"
VOLUME_OUTLINE_MIN_HAN = 500
VOLUME_OUTLINE_SIGNED_FIELDS = (
    "volume", "title", "spine", "goal", "word_budget", "outline", "plot_role",
    "inherits", "advances", "ending_direction", "next_handoff",
)
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")


def outline_han_count(value: Any) -> int:
    return len(_HAN.findall(value)) if isinstance(value, str) else 0


def volume_outline_key(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    match = re.fullmatch(r"(?:vol-)?0*([1-9][0-9]*)", str(value))
    return f"vol-{int(match[1]):04d}" if match else None


def _error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **fields}


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _signed_outline_hash(volumes: dict[str, Any], ids: list[str]) -> str:
    payload = [{"id": ident, **{field: volumes.get(ident, {}).get(field)
                               for field in VOLUME_OUTLINE_SIGNED_FIELDS}} for ident in ids]
    return "sha256:" + sha256_text(canonical_json(payload).decode("utf-8"))


def sign_volume_outline_contract(volumes: dict[str, Any], book_words: int, chapter_target: int) -> dict[str, Any]:
    """Seal already-authored volumes; never generate missing plot or summaries."""
    ids = [f"vol-{number:04d}" for number in range(1, len(volumes) + 1)]
    return {"schema": VOLUME_OUTLINE_SCHEMA, "volume_count": len(ids), "volume_ids": ids,
            "book_words": book_words, "chapter_words_target": chapter_target,
            "signed_total_chapters": expected_total_chapters(book_words, chapter_target),
            "signed_volume_chapters": sum(int(volumes[ident]["chapters_budget"]) for ident in ids),
            "signed_outlines_hash": _signed_outline_hash(volumes, ids)}


def volume_outline_errors(plan: dict[str, Any], *, required: bool = False) -> list[dict[str, Any]]:
    """Validate all signed volume outlines, their book budget, and their seal.

    Legacy plans without the version remain readable. New hatch projects retain
    a schema marker in both book_outline and config so removing the contract is
    diagnosable rather than a compatibility downgrade.
    """
    outline = plan.get("book_outline") or {}
    raw = plan.get("volume_outline_contract")
    required = required or isinstance(outline, dict) and outline.get("volume_outline_schema") == VOLUME_OUTLINE_SCHEMA
    if raw is None:
        return [_error("volume_outline_contract_missing", "sign the complete whole-book volume outline before chapter planning")] if required else []
    if not isinstance(raw, dict) or raw.get("schema") != VOLUME_OUTLINE_SCHEMA:
        return [_error("volume_outline_contract_invalid", f"volume_outline_contract must declare {VOLUME_OUTLINE_SCHEMA}")]
    count = raw.get("volume_count")
    ids = raw.get("volume_ids")
    if not _positive_int(count) or not isinstance(ids, list) or len(ids) != count or ids != [f"vol-{number:04d}" for number in range(1, count + 1)]:
        return [_error("volume_outline_registry_invalid", "volume_count and ordered volume_ids must cover every volume from 1 to N")]
    volumes = plan.get("volumes")
    if not isinstance(volumes, dict) or set(volumes) != set(ids):
        return [_error("volume_outline_registry_incomplete", "all signed volumes must exist exactly once; unknown or deleted volumes are forbidden", expected=ids)]
    errors = []
    pace = raw.get("chapter_words_target")
    words = raw.get("book_words")
    if not _positive_int(pace) or not _positive_int(words):
        errors.append(_error("volume_outline_budget_invalid", "signed book_words and chapter_words_target must be positive integers"))
    word_sum = chapter_sum = 0
    authored_outlines: dict[str, str] = {}
    for number, ident in enumerate(ids, 1):
        entry = volumes[ident]
        if not isinstance(entry, dict):
            errors.append(_error("volume_outline_invalid", "each volume must be an object", volume=ident))
            continue
        if entry.get("volume") != number or isinstance(entry.get("volume"), bool):
            errors.append(_error("volume_outline_order_invalid", "volume number must match its signed registry position", volume=ident))
        for field in VOLUME_OUTLINE_SIGNED_FIELDS:
            if field in {"volume", "word_budget"}:
                continue
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                errors.append(_error("volume_outline_field_missing", f"{ident}.{field} requires explicit authored wording", volume=ident, field=field))
        han = outline_han_count(entry.get("outline"))
        if han < VOLUME_OUTLINE_MIN_HAN:
            errors.append(_error("volume_outline_too_short", "each volume outline must contain at least 500 Han characters; punctuation and Latin text do not count", volume=ident, han_count=han, minimum=VOLUME_OUTLINE_MIN_HAN))
        if isinstance(entry.get("outline"), str):
            normalized = re.sub(r"\s+", "", entry["outline"])
            if normalized in authored_outlines:
                errors.append(_error("volume_outline_reused", "each volume needs its own authored outline; the same outline cannot be reused for multiple volumes", volume=ident, reused_from=authored_outlines[normalized]))
            elif normalized:
                authored_outlines[normalized] = ident
        for field in ("word_budget", "chapters_budget"):
            if not _positive_int(entry.get(field)):
                errors.append(_error("volume_outline_budget_invalid", f"{ident}.{field} must be a positive integer", volume=ident, field=field))
        if _positive_int(entry.get("word_budget")):
            word_sum += entry["word_budget"]
        if _positive_int(entry.get("chapters_budget")):
            chapter_sum += entry["chapters_budget"]
        if _positive_int(pace) and _positive_int(entry.get("word_budget")) and _positive_int(entry.get("chapters_budget")):
            expected = expected_total_chapters(entry["word_budget"], pace)
            rebudget = outline.get("chapter_rebudget") or {} if isinstance(outline, dict) else {}
            if rebudget.get("continuation_volume") == ident:
                expected += int(rebudget.get("additional_chapters") or 0)
            if entry["chapters_budget"] != expected:
                errors.append(_error("volume_outline_chapters_mismatch", "volume chapter budget must retain its signed pace and any recorded continuation allowance", volume=ident, expected=expected))
    if words != word_sum or isinstance(outline, dict) and outline.get("book_words") != words:
        errors.append(_error("volume_outline_book_budget_mismatch", "all volume word budgets must exactly total the signed book target", sum_volume_words=word_sum, book_words=words))
    if _positive_int(pace) and _positive_int(words):
        signed_total = expected_total_chapters(words, pace)
        signed_volume_total = sum(expected_total_chapters(v["word_budget"], pace) for v in volumes.values()
                                  if isinstance(v, dict) and _positive_int(v.get("word_budget")))
        if raw.get("signed_total_chapters") != signed_total or raw.get("signed_volume_chapters") != signed_volume_total or not signed_total <= signed_volume_total < signed_total + count:
            errors.append(_error("volume_outline_chapter_rounding_invalid", "record the exact per-volume ceiling sum; its rounding surplus must be less than the number of volumes"))
    if all(isinstance(volumes[ident], dict) for ident in ids) and raw.get("signed_outlines_hash") != _signed_outline_hash(volumes, ids):
        errors.append(_error("volume_outline_contract_drift", "whole-book volume obligations were changed after signing; plan extend may only add detail_outline/recap within the existing agreement"))
    declared_volumes = set()
    for act in outline.get("acts") or [] if isinstance(outline, dict) else []:
        if not isinstance(act, dict):
            continue
        value = act.get("volumes")
        values = value if isinstance(value, list) else str(value or "").replace("，", ",").split(",")
        for token in values:
            token = str(token).strip()
            span = re.fullmatch(r"([0-9]+)\s*[-–至]\s*([0-9]+)", token)
            if span:
                start, end = map(int, span.groups())
                if 1 <= start <= end <= count:
                    declared_volumes.update(range(start, end + 1))
                else:
                    errors.append(_error("volume_outline_act_range_invalid", "act volume range lies outside the signed whole-book registry", volumes=token))
            elif (key := volume_outline_key(token)) in ids:
                declared_volumes.add(ids.index(key) + 1)
            else:
                errors.append(_error("volume_outline_act_range_invalid", "act volume is not in the signed whole-book registry", volumes=token))
    if declared_volumes != set(range(1, count + 1)):
        errors.append(_error("volume_outline_act_coverage_missing", "act ranges must cover every signed volume", covered=sorted(declared_volumes)))
    for chapter in plan.get("chapters") or []:
        if isinstance(chapter, dict) and volume_outline_key(chapter.get("volume", 1)) not in ids:
            errors.append(_error("volume_outline_unknown_chapter_volume", "chapter points outside the signed volume registry", chapter=chapter.get("chapter"), volume=chapter.get("volume")))
    return errors


def volume_outline_warnings(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("volume_outline_contract") is not None:
        return []
    volumes = plan.get("volumes") if isinstance(plan.get("volumes"), dict) else {}
    return [_error("volume_outline_legacy_unsigned", "legacy book has no signed complete volume-outline version; report missing volume outlines and supplement them before changing the book direction", missing_volumes=[str(key) for key, value in volumes.items()
                                                                                                                       if not isinstance(value, dict) or outline_han_count(value.get("outline")) < VOLUME_OUTLINE_MIN_HAN])]


def volume_outline_view(plan: dict[str, Any], volume: Any = None) -> dict[str, Any]:
    """Index every volume without duplicating prose; return one selected full outline."""
    volumes = plan.get("volumes") if isinstance(plan.get("volumes"), dict) else {}
    registry = plan.get("volume_outline_contract") if isinstance(plan.get("volume_outline_contract"), dict) else {}
    ids = registry.get("volume_ids") or list(volumes)
    key = volume_outline_key(volume) if volume is not None else None
    if volume is not None and key not in volumes:
        # Legacy aliases use the same lookup as chapter packs and volume budgets.
        from .store import volume_entry_for
        entry = volume_entry_for(volumes, volume)
        key = next((ident for ident, value in volumes.items() if value is entry), None) if entry is not None else None
    index = []
    chapter_start = 1
    fields = ("volume", "title", "plot_role", "word_budget", "chapters_budget", "inherits", "advances", "ending_direction", "next_handoff", "goal", "spine")
    for ident in ids:
        if not isinstance(volumes.get(ident), dict):
            continue
        entry = volumes[ident]
        budget = entry.get("chapters_budget") if _positive_int(entry.get("chapters_budget")) else 0
        index.append({"id": ident, "path": f"plan.volumes.{ident}", "outline_path": f"plan.volumes.{ident}.outline",
                      **{field: entry.get(field) for field in fields},
                      "field_paths": {field: f"plan.volumes.{ident}.{field}" for field in (*fields, "outline")},
                      "chapter_start": chapter_start, "chapter_end": chapter_start + budget - 1,
                      "outline_han_count": outline_han_count(entry.get("outline"))})
        chapter_start += budget
    return {"schema": VOLUME_OUTLINE_SCHEMA, "mode": "signed" if registry else "legacy_unsigned",
            "contract": copy.deepcopy(registry), "index": index,
            "selected": {"id": key, "path": f"plan.volumes.{key}", **copy.deepcopy(volumes[key])} if key in volumes else None,
            "warnings": volume_outline_warnings(plan)}
