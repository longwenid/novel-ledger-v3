"""文风内容层：把 `references/voices/<id>.md` 解析成项目 voice profile。

职责边界：本模块管**文风从哪来**（解析所选手册 → 装配 profile → 落 `book/memory/voice.json`），
`voice_pack.py` 管**文风怎么进 pack**。依赖单向：voice_manual（内容）→ voice_pack（装配）→ pack。

项目一次只选一套文风。开书时由 v3 manifest 明确选择；开书后换套用 `voice apply --voice`。
手册是唯一来源，pack 是唯一出口。**没有任何脚本给文字质感打分**——质感由组装自检（对着手册判据）与 polish
机检硬红线按域把关；解析出的清单与概念保存在项目文风档案和章节包中。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..infra.util import LedgerError, ok

DEFAULT_VOICE_ID = "shijing"

# 手册解析契约见 `.dev/architecture.md`「易踩的隐式契约」。正则都**行首锚定**：
# - `^##` 排除三级标题，否则手册里带数字编号的 `### N.` 小节会让同一语义被抓两遍
#   （历史上文末内联了一份 `### 1. 视角与叙述者` 的分析报告，正是这么中招的；那份报告
#   已移到 `.dev/wenfeng-analysis.md`，但锚定保留——它是防下一次的，不是修上一次的）；
# - 自检清单的终止条件收到 `\n---|\n#`，否则会一路吞掉分隔线和下一个一级标题；
# - 清单条目只认「数字编号 + 点号」开头的行，不靠跟手册条数耦合的魔数截断；
# - 自检标题精确为「自检」，硬禁标题精确为「硬禁」——不跟某一套文风的编号/措辞耦合。
_CHECKLIST_SEC_RE = re.compile(r"(?m)^##\s*自检\s*\n(.*?)(?=\n---|\n#|\Z)", re.DOTALL)
_CHECKLIST_ITEM_RE = re.compile(r"^\d+\.\s*(.+)$")
_CONCEPT_SEC_RE = re.compile(r"(?m)^##\s*\d+\.\s*([^\n]+)\n(.*?)(?=\n##|\Z)", re.DOTALL)
_HARD_BAN_SEC_RE = re.compile(r"(?m)^###\s*硬禁\s*\n(.*?)(?=\n###|\n##|\Z)", re.DOTALL)
_VOICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def skill_root() -> Path:
    """novel-ledger skill 根目录。

    用 absolute() 不用 resolve()：项目里 `.cursor/skills/novel-ledger` 常常是
    指向另一棵工作树的 symlink。resolve() 会把 pack.voice_skill_manual 显示成
    AndroidStudioProjects/...，终审编辑 会当成过期副本去开错文件。
    读文件仍走这份入口（symlink 可打开）；对外路径停在当前项目的 skill 目录。
    """
    return Path(__file__).absolute().parents[3]


def voices_dir() -> Path:
    return skill_root() / "references" / "voices"


def find_voice_path(voice_id: str) -> Path | None:
    """寻找文风手册路径：优先查找外部独立的 `voice-<id>` 或 `voice_<id>` Skill，其次查找内置 `references/voices/<id>.md`。"""
    vid = (voice_id or "").strip()
    if not _VOICE_ID_RE.fullmatch(vid):
        return None
    skills_dir = skill_root().parent
    # 1. 查找外部独立的子文风 Skill: 如 .agent/skills/voice-wulong/SKILL.md
    for external_name in (f"voice-{vid}", f"voice_{vid}", vid):
        ext_skill_md = skills_dir / external_name / "SKILL.md"
        if ext_skill_md.is_file():
            return ext_skill_md.absolute()
        ext_manual_md = skills_dir / external_name / "references" / "manual.md"
        if ext_manual_md.is_file():
            return ext_manual_md.absolute()
    # 2. 查找内置 references/voices/<id>.md
    internal_path = voices_dir() / f"{vid}.md"
    if internal_path.is_file():
        return internal_path.absolute()
    return None


def resolve_voice_id(voice_id: str) -> str:
    """校验 id 对应文风手册存在（外部独立 Skill 或内置手册）。非法或不存在都报 `unknown_voice`。"""
    vid = (voice_id or "").strip()
    if not _VOICE_ID_RE.fullmatch(vid):
        raise LedgerError("unknown_voice", f"未知文风: {voice_id!r}", {"voice_id": voice_id})
    path = find_voice_path(vid)
    if not path or not path.is_file():
        expected_internal = voices_dir() / f"{vid}.md"
        expected_external = skill_root().parent / f"voice-{vid}" / "SKILL.md"
        raise LedgerError(
            "unknown_voice",
            f"未知文风: {vid}（未找到外部技能 {expected_external} 或内置手册 {expected_internal}）",
            {"voice_id": vid, "expected_external": str(expected_external), "expected_internal": str(expected_internal)},
        )
    return vid


def voice_manual_path(voice_id: str) -> Path:
    vid = resolve_voice_id(voice_id)
    path = find_voice_path(vid)
    if not path:
        raise LedgerError("missing_voice_manual", f"文风手册不存在: {voice_id}")
    return path


def _read_manual(path: Path, *, label: str, voice_id: str, optional: bool = False) -> tuple[str, str]:
    """读一份手册并做空文件校验；三个 load_* 共用。"""
    if not path.is_file():
        if optional:
            return "", ""
        raise LedgerError("missing_voice_manual", f"{label}不存在: {voice_id}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise LedgerError(
            "invalid_voice_manual",
            f"{label}为空: {path}",
            {"voice_id": voice_id, "manual": str(path)},
        )
    return str(path), text


def load_live_manual(voice_id: str) -> tuple[str, str]:
    """从外部独立 Skill 或内置手册读总手册全文。pack 装配与指纹计算走当前文风 id，禁止用 voice.json 里冻结的绝对路径。

    项目若用另一份 skill 副本 init，profile.skill_manual 会冻结那份绝对路径。
    润色阶段 / ack 若再去打开冻结路径，文风就跟当前仓库脱节。voice_id 才是真源。
    skill 目录若是 symlink，对外路径停在入口（absolute），不跟进 resolve 目标。
    """
    return _read_manual(voice_manual_path(voice_id), label="文风手册", voice_id=voice_id)


def load_writing_manual(voice_id: str) -> tuple[str, str]:
    """读取给润色编辑的文风手册；内置文风直接使用总手册。

    外部或自定义文风仍可提供 `<id>.writer.md` companion；有 companion
    时将其交给润色编辑，总手册仍是解析真源。
    """
    path = voice_manual_path(voice_id)
    write_path = path.parent / f"{path.stem}.writer.md"
    chosen = write_path if write_path.is_file() else path
    return _read_manual(chosen, label="润色文风手册", voice_id=voice_id)


def load_content_manual(voice_id: str) -> tuple[str, str]:
    """读取给执笔编辑的内容层手册，指导场景与对白如何实现所选文风。

    companion 命名：`references/voices/<id>.content.md`。
    如果没有对应的 .content.md，返回空字符串（意味着该文风不提供内容层指引）。

    按阶段分发：
    - draft 视图注入 content.md（执笔编辑按项目正典写场景）
    - polish 视图注入总手册；外部或自定义文风有 writer.md 时改用它
    - 总手册始终是解析真源，阶段手册的内容摘要均参与指纹
    """
    path = voice_manual_path(voice_id)
    content_path = path.parent / f"{path.stem}.content.md"
    return _read_manual(content_path, label="内容层手册", voice_id=voice_id, optional=True)


def iter_voice_manuals() -> list[tuple[str, Path]]:
    """扫描所有合法的文风手册：包括外部独立 `voice-*` 技能和内置 `references/voices/*.md`。"""
    found: dict[str, Path] = {}
    skills_dir = skill_root().parent
    # 1. 扫描外部独立子文风技能目录: 如 voice-wulong -> id: wulong
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.glob("voice-*")):
            if skill_dir.is_dir():
                vid = skill_dir.name[6:]  # 去掉 "voice-" 前缀
                if _VOICE_ID_RE.fullmatch(vid):
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.is_file():
                        found[vid] = skill_md.absolute()
    # 2. 扫描内置 references/voices/*.md
    if voices_dir().is_dir():
        for path in sorted(voices_dir().glob("*.md")):
            vid = path.stem
            if _VOICE_ID_RE.fullmatch(vid) and vid not in found:
                found[vid] = path.absolute()
    return sorted(found.items(), key=lambda x: x[0])


def list_voices() -> list[dict[str, str]]:
    """列出所有可用手册：id + 一级标题 + 绝对路径。"""
    items: list[dict[str, str]] = []
    for vid, path in iter_voice_manuals():
        title = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
        items.append({"id": vid, "title": title, "manual": str(path.resolve())})
    return items


def parse_skill_md(path: Path) -> dict[str, Any]:
    """解析文风手册，提取公式/概念/自检清单。加条数校验，避免静默抽空。

    随包文风的最低结构要求：
    - 公式：必须非空
    - 自检清单：≥5 条
    - 概念：≥8 条

    低于阈值时报 `invalid_voice_manual_structure`，附带诊断信息。
    """
    if not path.is_file():
        raise LedgerError("missing_voice_manual", f"文风手册不存在: {path}")
    text = path.read_text(encoding="utf-8")
    formula = ""
    m = re.search(r">\s*\*\*(.+?)\*\*", text, re.DOTALL)
    if m:
        formula = re.sub(r"\s+", " ", m.group(1)).strip()

    checklist: list[str] = []
    sec = _CHECKLIST_SEC_RE.search(text)
    if sec:
        for line in sec.group(1).splitlines():
            m_item = _CHECKLIST_ITEM_RE.match(line.strip())
            if m_item and m_item.group(1).strip():
                checklist.append(m_item.group(1).strip())

    concepts: list[str] = []
    concept_sections_found = 0
    for sec_m in _CONCEPT_SEC_RE.finditer(text):
        title = sec_m.group(1).strip()
        concept_sections_found += 1
        if title.startswith("交付前") or title.startswith("提笔前") or title.startswith("自检"):
            continue
        for line in sec_m.group(2).splitlines():
            if line.startswith("- "):
                item = line[2:].strip()
                if item and not item.startswith("对：") and not item.startswith("错："):
                    concepts.append(item)
                    break

    hard_concepts: list[str] = []
    hard_lead: str | None = None
    hard_sec = _HARD_BAN_SEC_RE.search(text)
    if hard_sec:
        for line in hard_sec.group(1).splitlines():
            item = line[2:].strip() if line.startswith("- ") else ""
            if not item or item.startswith("错：") or item.startswith("对："):
                continue
            if item.startswith("**硬禁**"):
                cleaned = re.sub(r"\*\*硬禁\*\*：?", "", item).strip()
                if hard_lead is None:
                    hard_lead = cleaned  # 只把第一条「硬禁」提为首位，其余按原文顺序保留
                else:
                    hard_concepts.append(cleaned)
            else:
                hard_concepts.append(item)
    if hard_lead:
        hard_concepts.insert(0, hard_lead)
    concepts = hard_concepts + concepts

    # 条数校验：仅对内置随包文风手册严格拦截，避免静默抽空
    is_bundled = False
    try:
        is_bundled = path.resolve().parent == voices_dir().resolve()
    except Exception:
        pass

    if is_bundled:
        MIN_CHECKLIST = 5
        MIN_CONCEPTS = 8
        issues = []
        if not formula:
            issues.append("公式为空（预期：引用块内加粗文本 > **公式**）")
        if len(checklist) < MIN_CHECKLIST:
            issues.append(f"自检清单只抽到 {len(checklist)} 条（预期 ≥{MIN_CHECKLIST}）：标题必须精确为「## 自检」")
        if len(concepts) < MIN_CONCEPTS:
            issues.append(
                f"概念只抽到 {len(concepts)} 条（预期 ≥{MIN_CONCEPTS}）：需要 ≥3 个「## 数字.」编号小节，"
                f"且「### 硬禁」小节必须有内容。当前匹配到 {concept_sections_found} 个编号小节"
            )

        if issues:
            raise LedgerError(
                "invalid_voice_manual_structure",
                f"文风手册 {path.name} 结构异常，解析结果低于最小阈值。"
                "可能是标题被改动或内容被删除。见 .dev/architecture.md「易踩的隐式契约」。\n"
                + "\n".join(f"  • {x}" for x in issues),
                {
                    "manual": str(path),
                    "formula_ok": bool(formula),
                    "checklist_count": len(checklist),
                    "concepts_count": len(concepts),
                    "concept_sections_found": concept_sections_found,
                    "min_checklist": MIN_CHECKLIST,
                    "min_concepts": MIN_CONCEPTS,
                },
            )

    return {"style_formula": formula, "checklist": checklist, "concepts": concepts}


def build_voice_profile(
    *,
    voice_id: str,
    pack_cap: int,
    cap: int | None = None,
) -> tuple[dict[str, Any], int]:
    """解析所选手册并保留全部概念，装配项目 voice profile。**不写盘、不碰 store**。

    返回 `(profile, 手册里解析出的概念总条数)`——后者只用于回报 `concepts_dropped`，
    不进 profile（voice.json 的字段表见 references/state-contract.md）。

    `init` 与 `voice apply` 共用这一条装配路径：profile 是项目里文风的唯一来源，
    两处各写一遍装配逻辑迟早漂成两套。不写盘是为了让 `init` 能在**任何文件落地之前**
    先验证手册可解析，失败时不留下半个项目。
    """
    vid = resolve_voice_id(voice_id)
    manual_path = voice_manual_path(vid)
    meta = parse_skill_md(manual_path)
    parsed_concepts = [str(x).strip() for x in meta["concepts"] if str(x).strip()]
    checklist = [str(x).strip() for x in meta["checklist"] if str(x).strip()]
    formula = str(meta.get("style_formula") or "").strip()
    # 解析空了就地停住。profile 是 pack 里概念/公式/自检的唯一来源，
    # 写进空 `concepts` 或空公式等于让写者裸奔，而且零告警。
    if not parsed_concepts or not checklist or not formula:
        raise LedgerError(
            "invalid_voice_manual",
            f"文风手册解析为空（公式 {'有' if formula else '无'} / "
            f"概念 {len(parsed_concepts)} 条 / 自检 {len(checklist)} 条），"
            "拒绝写入项目文风：文风是 pack 里概念/公式/自检的唯一来源，写进去只会让写者裸奔而不报错。"
            f"多半是 {manual_path.name} 的标题结构被改坏了——见 .dev/architecture.md「易踩的隐式契约」。",
            {
                "voice_id": vid,
                "manual": str(manual_path.resolve()),
                "formula": bool(formula),
                "concepts": len(parsed_concepts),
                "checklist": len(checklist),
            },
        )
    # The selected manual is one relevant source; retain all of its concepts.
    # Legacy --cap and configuration values no longer delete selected material.
    concepts = list(parsed_concepts)
    cap_effective = len(concepts)

    profile = {
        "schema": "novel-ledger.voice-memory.v1",
        "voice_id": vid,
        "skill_manual": str(manual_path.resolve()),
        "applied_at": int(time.time()),
        "concept_cap": cap_effective,
        "style_formula": formula,
        "concepts": concepts,
        "checklist": checklist,
    }
    return profile, len(parsed_concepts)


def apply_voice(
    project_store: Any,
    *,
    voice_id: str | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    from ..infra.store import BookStore

    if not isinstance(project_store, BookStore):
        raise LedgerError("invalid_store", "apply_voice requires BookStore")

    requested = (voice_id or "").strip()
    if requested:
        vid = resolve_voice_id(requested)
    else:
        existing = str((project_store.load_voice_profile() or {}).get("voice_id") or "").strip()
        if not existing:
            raise LedgerError(
                "unknown_voice",
                "项目未记录 voice_id，请显式传入 --voice <id>",
                {"voice_path": str(project_store.voice_memory_path)},
            )
        vid = resolve_voice_id(existing)

    pack_cap = int(project_store.load_config()["pack_caps"]["voice_concepts"])
    profile, parsed_count = build_voice_profile(voice_id=vid, pack_cap=pack_cap, cap=cap)
    concepts = profile["concepts"]
    checklist = profile["checklist"]
    cap_effective = int(profile["concept_cap"])
    cap_requested = pack_cap if cap is None else int(cap)
    # 会话笔记跨 apply 保留：它是另一条数据流（ack 回灌），重跑 apply / 换套都不该静默清空它。
    session_notes = project_store.load_voice_session_notes()
    if session_notes:
        profile["session_notes"] = session_notes
    project_store.save_voice_profile(profile)

    return ok(
        action="voice_apply",
        voice_id=vid,
        project=str(project_store.project.resolve()),
        voice_path=str(project_store.voice_memory_path.resolve()),
        skill_manual=profile["skill_manual"],
        concept_count=len(concepts),
        concept_cap=cap_effective,
        concept_cap_requested=cap_requested,
        pack_concept_cap=pack_cap,
        concepts_dropped=parsed_count - len(concepts),
        concepts=concepts,
        style_formula=profile["style_formula"],
        checklist_count=len(checklist),
        session_notes_kept=len(session_notes),
    )
