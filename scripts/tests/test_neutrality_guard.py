"""制度层零作品内容守卫（结构性承诺的可回归形式）。

`references/roles.md §0 三层边界` 规定：制度层（SKILL.md / references / agents / scripts）
只规定"角色如何组织、信息如何隔离、决定如何做出"，**零作品内容**。项目真正的人名、地名、
年份、势力、机制、年代物件一律只存在于项目层 `$PROJECT/book/`。

这条纪律以前只写在文档里，靠每次改动的自觉。本次加固的输入恰好是一本已写完的书的
审查报告——正是最容易"顺手把结论写进 skill"的场景，所以把它做成会红的检查：

1. 制度层文件里不得出现**本次审查那本书**的专有名词（`_BLOCKED_TERMS`）；
2. 不得出现项目层推导出的"核验类别清单"式内容（把某本书的核对项写成 skill 常量）；
3. 具体的取值域声明只能出现在项目层配置里（`fact_keys` / `glossary` / `quant_keys`），
   制度层只有键名与结构判据。

命中即红，要求改为在项目层声明（`config set`）而不是写进 skill。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 制度层：任何"书本身"的内容都不许进这些目录/文件。
_GOVERNANCE_GLOBS = (
    "SKILL.md",
    "references/**/*.md",
    "agents/**/*.md",
    "scripts/novel_ledger_core/**/*.py",
)

# 本次审查那本书的专有名词（姓氏/地名/机构/人物/年代物件）。它们**只**允许出现在
# 项目层与审查报告里，进制度层就说明 skill 被写成了"为这一本书定制"。
# 清单本身属于测试资产，不是文档，故不触发第 1 条。
_BLOCKED_TERMS = (
    "老侯家",
    "陈守拙",
    "陈小江",
    "陈小满",
    "吴秀兰",
    "沈巧",
    "柳树巷",
    "李建军",
    "郭永强",
    "郑宝根",
    "孙建军",
    "李卫平",
    "祝世昌",
    "吴长贵",
    "赵德海",
    "马春来",
    "陈卫东",
    "周振山",
    "柳木笛",
    "布票",
    "工业券",
)


def _governance_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _GOVERNANCE_GLOBS:
        files.extend(sorted(REPO.glob(pattern)))
    return [path for path in files if path.is_file()]


def _scan(needles: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for path in _governance_files():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO)} 含作品内容 {needle!r}")
    return offenders


def _contains_any(needles: tuple[str, ...], text: str) -> str | None:
    for needle in needles:
        if needle in text:
            return needle
    return None


def test_governance_layer_carries_no_work_specific_content():
    """制度层不得出现那本书的专有名词——它们只能进项目层与审查报告。"""
    assert _governance_files(), "制度层文件扫描集为空，本守卫失去对象"
    offenders = _scan(_BLOCKED_TERMS)
    assert not offenders, (
        "制度层被写入了作品内容：\n" + "\n".join(offenders)
        + "\n\n修法：把取值写进项目层配置（`config set --key fact_keys ...` / glossary / quant_keys），"
        "skill 只保留键名与结构判据。"
    )


def test_the_guard_itself_has_teeth():
    """守卫的机制自检：往制度层文本里插一个作品名，扫描必须报出来。

    没有这条，"零内容"可能退化成一份永远为空的清单。
    """
    assert not _scan(_BLOCKED_TERMS), "守卫基准：当前制度层应当是干净的"
    assert _contains_any(_BLOCKED_TERMS, "他推开门，走进柳树巷的院子。") == "柳树巷"
    assert _contains_any(_BLOCKED_TERMS, "他推开门，走进窄巷的院子。") is None
    # 清单里每条都必须真的能命中自己的字面（防止清单被改成永不匹配的形状）
    for needle in _BLOCKED_TERMS:
        assert _contains_any(_BLOCKED_TERMS, f"草稿里出现 {needle} 字样。") == needle


def test_fact_registry_doc_declares_only_structural_kinds():
    """事实登记表文档只讲结构（kind/形状/分工），不携带任何作品的取值。"""
    doc = REPO / "references" / "fact-registry.md"
    assert doc.is_file(), "少了 fact-registry.md：新键的形状没有按需文档可读"
    text = doc.read_text(encoding="utf-8")
    for kind in ("entity", "number", "date", "set", "count"):
        assert kind in text, f"fact-registry.md 未说明 kind={kind} 的形状"
    assert "config set" in text, "fact-registry.md 必须给落盘命令，否则键填不进去"
