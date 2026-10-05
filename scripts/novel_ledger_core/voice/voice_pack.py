"""文风内容进 pack 的装配层。

职责边界：`voice_manual.py` 管**文风从哪来**（解析 `references/voices/<id>.md` 装配 profile），
本模块只管**文风怎么进 pack**——把项目 voice profile 的公式/概念/自检、手册路径与内文
及会话笔记装配为候选字段。依赖单向：voice_manual（内容）→ 本模块（装配）
→ pack（工作集）。本模块是纯函数、不碰磁盘也不碰 store，所以 `pack.py` 引它没有任何写路径负担。
总手册内文由 `pack.py` 经 `load_live_manual` 读入后传入，本模块不自己打开文件。

这是 pack 文风元数据的**唯一**装配入口。总手册供开书、切换文风时解析公式、概念和自检，
内置文风也用它指导润色。常规章节的 canonical pack 携带内容层与润色手册内文：
draft 视图给执笔内容层，polish 视图给润色手册。外部或自定义文风可单独提供
writer companion；路径不能代替阶段视图中的内文。
"""

from __future__ import annotations

from typing import Any

_INSTRUCTION_LEAD = (
    "通读 pack.voice_writing_text（润色文风手册），据此润色本章。"
)

_CONTENT_INSTRUCTION_LEAD = (
    "通读 pack.voice_content_text（内容层手册），据此写草稿。"
)

def voice_for_pack(
    concepts_in: list[str] | None = None,
    *,
    session_notes: list[str] | None = None,
    cap: int = 14,
    profile: dict[str, Any] | None = None,
    skill_manual: str | None = None,
    manual_text: str | None = None,
) -> dict[str, Any]:
    """装配 pack 的文风元数据与总手册候选字段。

    两个来源分开进、顺序即优先级：`concepts_in`（`init` 与 `voice apply` 从所选手册
    解析的长驻概念）和 `session_notes`（ack `--voice-note` 与写者 `memory.voice_concepts`
    回灌的笔记）去重后完整保留；旧cap参数不再删材料。具体阶段仍只收到自己的手册。

    `skill_manual` / `manual_text` 由调用方从**当前** skill_root 解析后传入，覆盖
    profile 里可能过期的绝对路径。有全文时 instruction = 主任务说明（精炼）；
    总手册内文由本函数返回给调用方。调用方将选中的润色手册写入
    `voice_writing_text`；内置文风的润色手册就是总手册。概念与自检清单留在
    canonical 元数据中，不在 instruction 中复述成另一份阶段规则。
    """
    concepts: list[str] = []
    seen: set[str] = set()

    def _take(items: list[str]) -> None:
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            concepts.append(text)

    _take(list(concepts_in or []))
    _take(list(session_notes or []))

    prof = profile or {}
    live_path = str(skill_manual or "").strip() or str(prof.get("skill_manual") or "").strip()
    live_text = str(manual_text or "").strip()
    formula = str(prof.get("style_formula") or "").strip()
    instruction_parts: list[str] = [_INSTRUCTION_LEAD]

    checklist_out: list[str] = []
    raw_checklist = prof.get("checklist")
    if isinstance(raw_checklist, list):
        for item in raw_checklist:
            text = str(item).strip()
            if not text:
                continue
            checklist_out.append(text)

    # 有总手册内文时，instruction 保持精炼；实际进入哪个 pack 字段由调用方决定。
    if not live_text:
        if live_path:
            instruction_parts.append(f"完整文风手册：{live_path}")
        if formula:
            instruction_parts.append(formula)
        instruction_parts.extend(concepts)
        for item in checklist_out:
            instruction_parts.append(f"【自检】{item}")

    return {
        "instruction": "\n".join(instruction_parts),
        "concepts": concepts,
        "style_formula": formula,
        "checklist": checklist_out,
        "skill_manual": live_path,
        "manual_text": live_text,
    }


def content_for_pack(
    *,
    profile: dict[str, Any] | None = None,
    skill_manual: str | None = None,
    content_text: str | None = None,
) -> dict[str, Any]:
    """装配给 draft 视图的内容层字段。

    内容层手册（如 shijing.content.md）指导本风格的场景、行动与对白，
    仍以项目正典决定人物和世界事实。draft 视图注入内容层，
    polish 视图注入润色手册；两阶段不同时读取两份内文。

    返回字段：
    - content_instruction: 精炼指令
    - content_skill_manual: 内容层手册路径
    - content_text: 内容层手册全文
    """
    live_path = str(skill_manual or "").strip()
    live_text = str(content_text or "").strip()

    instruction_parts: list[str] = [_CONTENT_INSTRUCTION_LEAD]

    # 内容层内文由调用方独立注入 voice_content_text，instruction 不重复拼接。
    if not live_text and live_path:
        instruction_parts.append(f"内容层手册：{live_path}")

    return {
        "content_instruction": "\n".join(instruction_parts),
        "content_skill_manual": live_path,
        "content_text": live_text,
    }
