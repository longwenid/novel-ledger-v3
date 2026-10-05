"""流水线测试助手：draft → polish → assemble（单一 compact 泳线）。

流水线恒为 compact（无独立审校角色），助手只剩阶段直驱；
`decoupled_submit` / `advance_to_assembly` 供各用例按同一语义驱动流水线；
`make_plan` / `make_chapter` / `write_plan` 提供最小合法计划骨架，免去各文件抄重复字典。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from novel_ledger_core.control.pipeline import (
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore

FALLBACK_PROSE = "主角站在市集。拒收改期。凭据还压在柜台那头，改天再来取。"


def advance_to_assembly(store: BookStore, prose: str | None = None, chapter: int = 1) -> dict[str, Any]:
    """从当前 HEAD 一路推进到 await_assembly（机器闸由阶段函数内部执行）。"""
    text = (prose or "").strip() or FALLBACK_PROSE
    for _ in range(8):
        r = chapter_next(store)
        action = r.get("action")
        if action == "draft":
            Path(r["draft_output_path"]).write_text(text, encoding="utf-8")
            stage_draft_submit(store)
        elif action == "polish":
            Path(r["polished_output_path"]).write_text(text, encoding="utf-8")
            stage_polish_submit(store)
        elif action == "assemble":
            return r
        elif action in ("complete", "blocked", "extend_plan"):
            raise AssertionError(f"cannot advance from action={action}: {r}")
        else:
            raise AssertionError(f"unexpected action in advance_to_assembly: {r}")
    raise AssertionError("did not reach assemble stage")


def decoupled_submit(
    store: BookStore,
    output: dict[str, Any],
    prose: str | None = None,
    chapter: int = 1,
) -> dict[str, Any]:
    """把最终 output 推进到组装并 submit；未指定 prose 时用 output.prose（兜底正文）。"""
    text = prose
    if text is None:
        text = str(output.get("prose") or "").strip() or FALLBACK_PROSE
    advance_to_assembly(store, prose=text, chapter=chapter)
    return submit_output(store, output)


# ---------------------------------------------------------------------------
# 最小合法计划构造器
#
# 十几个测试文件原先各自抄一份 plan 字典（字段几乎逐字相同，差异只在章数与 tags），
# 改动一处要同步十几处。这里给出骨架，各文件只列**自己的差异**。
# ---------------------------------------------------------------------------

DEFAULT_BEATS: tuple[dict[str, Any], ...] = (
    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
)


def make_chapter(
    n: int = 1,
    *,
    location: str = "市集",
    present: tuple[str, ...] = ("主角", "掌柜"),
    tags: tuple[str, ...] | None = None,
    beats: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """一个最小合法章：默认「市集对峙」。传 None 表示不带该可选字段，**不是**用默认值。

    章级差异（volume / recap / 特定 beats）走 extra 与显式参数，别在调用点重抄整章。
    """
    ch: dict[str, Any] = {
        "chapter": n,
        "location": location,
        "present": list(present),
        "beats": [dict(b) for b in (DEFAULT_BEATS if beats is None else beats)],
    }
    if tags is not None:
        ch["tags"] = list(tags)
    ch.update(extra)
    return ch


def make_plan(
    chapters: int | list[dict[str, Any]] | None = None,
    *,
    volume_spine: str | None = "第一卷：主角要回那张被扣的凭据。",
    **extra: Any,
) -> dict[str, Any]:
    """最小合法 plan：`chapters` 传 int 生成 n 个标准章，传列表则原样使用。

    `volume_spine=None` 表示不带该键（有些用例专门验「只有章、没有卷脊」的路径）。
    """
    if chapters is None:
        chs = [make_chapter(1)]
    elif isinstance(chapters, int):
        chs = [make_chapter(i) for i in range(1, chapters + 1)]
    else:
        chs = list(chapters)
    plan: dict[str, Any] = {"title": "", "protagonist": "主角", "chapters": chs}
    if volume_spine is not None:
        plan["volume_spine"] = volume_spine
    plan.update(extra)
    return plan


def write_plan(tmp_path: Path, plan: dict[str, Any] | None = None, name: str = "plan.json") -> Path:
    """把 plan 落到 tmp_path（各测试文件的写盘步骤逐字相同，收成一处）。"""
    path = tmp_path / name
    path.write_text(json.dumps(make_plan() if plan is None else plan, ensure_ascii=False), encoding="utf-8")
    return path
