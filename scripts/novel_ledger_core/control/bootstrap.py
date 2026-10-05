"""开书引导：把 hatch 生成的计划与知识卡包装成一个可运行项目。

这是**控制层**的一部分：它必须同时碰配置、计划、正典与文风手册，而 `infra/store.py`
只能依赖 `infra/util.py`（见 `test_infra_layer_is_not_bypassed`）。所以
`init_project` 这类跨域编排留在这里，`BookStore` 留在 infra。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..infra.store import DEFAULT_CONFIG, BookStore, empty_head, validate_kb_cards
from ..infra.util import LedgerError, atomic_json, read_json

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
BACKGROUNDS_DIR = TEMPLATES_DIR / "backgrounds"


def _load_template_json(name: str) -> Any | None:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return None
    return read_json(path)


def list_backgrounds() -> list[str]:
    """Return bundled canon seed ids that contain at least one Markdown card."""
    if not BACKGROUNDS_DIR.is_dir():
        return []
    return [
        path.name
        for path in sorted(BACKGROUNDS_DIR.iterdir())
        if path.is_dir()
        and any(
            item.is_file() and item.suffix.lower() in {".md", ".markdown"}
            for item in path.iterdir()
        )
    ]


def resolve_background(name: str) -> Path:
    """Resolve a bundled canon seed without allowing path traversal."""
    slug = (name or "").strip()
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise LedgerError("invalid_background", "background id must be a simple slug")
    path = BACKGROUNDS_DIR / slug
    if not path.is_dir() or slug not in list_backgrounds():
        raise LedgerError(
            "unknown_background",
            f"unknown background: {slug}",
            {"known": list_backgrounds()},
        )
    return path


def copy_background_canon(src: Path, dest: Path) -> list[str]:
    """Copy the preset's canon cards; package documentation is not project canon."""
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for path in sorted(src.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        if path.name.lower() in {"readme.md", "license.md", "changelog.md"}:
            continue
        target = dest / path.name
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        copied.append(path.name)
    if not copied:
        raise LedgerError("empty_background", f"background has no canon markdown: {src}")
    return copied

def init_project(
    project: Path,
    *,
    plan_path: Path | None = None,
    kb_path: Path | None = None,
    protagonist: str = "",
    word_min: int | None = None,
    word_max: int | None = None,
    book_words: int | None = None,
    voice: str | None = None,
) -> dict[str, Any]:
    # 全书目标字数在**任何文件落地之前**先校验，像 missing_protagonist 一样把失败
    # 提前到开书时，避免留下一个半成型项目。
    if book_words is not None and int(book_words) <= 0:
        raise LedgerError("invalid_book_words", "book_words must be a positive integer")
    store = BookStore(project)
    if store.head_path.exists():
        raise LedgerError("already_initialized", f"project already has HEAD: {store.head_path}")

    # 文风在**任何文件落地之前**先装配好：一个没有文风概念的项目
    # 连第一章都开不出来（pack 的 missing_voice），与其让它半成型地存在，不如当场失败。
    # 未知 id / 手册解析坏掉时这里抛 unknown_voice / invalid_voice_manual，
    # 此刻还没 ensure_layout，不留下半个项目。
    from ..voice.voice_manual import DEFAULT_VOICE_ID, build_voice_profile

    voice_id = (voice or DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID
    voice_profile, _ = build_voice_profile(
        voice_id=voice_id,
        pack_cap=int(DEFAULT_CONFIG["pack_caps"]["voice_concepts"]),
    )

    # layout 延后到所有写盘前可判定的校验都通过之后（见下方 kb 块），
    # 保证 invalid_plan / missing_protagonist / invalid_kb 失败时不留半个项目。
    # deepcopy：cfg 的嵌套 dict 与 DEFAULT_CONFIG 隔离，避免 init 参数污染全局默认
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["require_ack"] = True
    if protagonist:
        cfg["protagonist"] = protagonist
    if word_min is not None:
        cfg["word_band"]["min"] = int(word_min)
    if word_max is not None:
        cfg["word_band"]["max"] = int(word_max)
    if book_words is not None:
        cfg["book_words"] = int(book_words)

    if plan_path:
        plan = read_json(Path(plan_path))
        if not isinstance(plan, dict) or not isinstance(plan.get("chapters"), list):
            raise LedgerError("invalid_plan", "plan JSON must contain a chapters list")
    else:
        plan = _load_template_json("plan.chapters.json") or {
            "title": "",
            "protagonist": protagonist,
            "volume_spine": "",
            "chapters": [],
        }
        if not isinstance(plan, dict) or not isinstance(plan.get("chapters"), list):
            raise LedgerError("invalid_plan", "plan template must contain a chapters list")
    if protagonist and not plan.get("protagonist"):
        plan["protagonist"] = protagonist
    if plan.get("protagonist") and not cfg.get("protagonist"):
        cfg["protagonist"] = plan["protagonist"]
    # 主角名是 NOW 卡主键：缺了它第一章就会以 missing_now_card 挂掉。把失败从
    # "跑到第一章" 提前到 "开书时"，此处尚未写 HEAD/config，项目还可重新 init。
    if not str(plan.get("protagonist") or "").strip():
        raise LedgerError(
            "missing_protagonist",
            "plan has no protagonist and --protagonist was not given; "
            "the NOW card needs a protagonist name (see references/plan-contract.md 章拍真源)",
            {"plan_path": str(plan_path) if plan_path else None},
        )
    # effects 稳定键防线与 plan extend 同一道：hatch 初签章拍（step6_beats）与导入的
    # plan 都经此入盘；坏键形（如 conditions 的 cond/status、items 的 item）若放到
    # 组装提交才暴露，只能 blocked 停线（真实项目曾各烧整夜一轮写链）。
    from ..infra.story_map import plan_shape_reject

    plan_shape_reject(plan.get("chapters") or [], origin="init/hatch")
    from ..infra.volume_outline import VOLUME_OUTLINE_SCHEMA, volume_outline_errors

    volume_errors = volume_outline_errors(plan)
    if volume_errors:
        raise LedgerError("volume_outline_contract_violation", "imported plan must retain its complete signed volume outlines", {"errors": volume_errors})
    volume_contract = plan.get("volume_outline_contract")
    if volume_contract is not None:
        signed_words = int(volume_contract["book_words"])
        if book_words is not None and int(book_words) != signed_words:
            raise LedgerError("volume_outline_contract_violation", "initialization must preserve the signed whole-book word target")
        cfg["book_words"] = signed_words
        cfg["volume_outline_schema"] = VOLUME_OUTLINE_SCHEMA
    if kb_path:
        raw = read_json(Path(kb_path))
        if isinstance(raw, dict) and isinstance(raw.get("cards"), list):
            cards = raw["cards"]
        elif isinstance(raw, list):
            cards = raw
        else:
            raise LedgerError("invalid_kb", "kb JSON must be {cards:[...]} or a list")
    else:
        raw = _load_template_json("kb.cards.json")
        if raw is None:
            cards = []
        elif isinstance(raw, dict) and isinstance(raw.get("cards"), list):
            cards = raw["cards"]
        elif isinstance(raw, list):
            cards = raw
        else:
            raise LedgerError("invalid_kb", "kb template must be {cards:[...]} or a list")

    # 在创建任何项目目录前验证到单张卡片；不能让合法外壳里的坏元素延迟到首章 pack 才崩。
    validate_kb_cards(cards)

    store.ensure_layout()
    store.save_config(cfg)
    store.save_plan(plan)
    # 初始化确定性分层记忆。此时还没有章节摘要，只写入空层级与指纹；后续每章 commit
    # 直接用已有 l1_summary 增量滚成阶段/卷/全书层，不增加任何模型请求。
    from ..content.hierarchical_memory import rebuild_hierarchical_memory

    rebuild_hierarchical_memory(store, 0)
    # 记下正典源指纹：没有它，`book audit` 就无法发现"改了 canon 却没 kb sync"
    # （那种情况下 cards.json 不变，canon_drift 也永远不会响）。
    from ..content.extract import canon_source_fingerprint

    store.save_kb(cards, source_fingerprint=canon_source_fingerprint(store.canon_dir) or None)
    store.write_head(empty_head())
    snapshot = _load_template_json("ledger.snapshot.json") or {
        "chapter": 0,
        "entities": {},
        "debts": [],
        "hooks": [],
        "relations": [],
        "occupancy": {},
    }
    if not isinstance(snapshot, dict):
        raise LedgerError("invalid_ledger", "ledger snapshot template must be an object")
    atomic_json(store.snapshot_path, snapshot)
    if not store.events_path.exists():
        store.events_path.write_text("", encoding="utf-8")
    store.save_voice_profile(voice_profile)
    payload = {
        "project": str(store.project),
        "book": str(store.book),
        "plan_chapters": len(plan.get("chapters") or []),
        "kb_cards": len(cards),
        "require_ack": True,
        "book_words": cfg["book_words"],
        "voice_id": voice_profile["voice_id"],
        "voice_manual": voice_profile["skill_manual"],
        "voice_concepts": len(voice_profile["concepts"]),
    }
    return payload
