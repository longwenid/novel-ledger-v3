"""文风节奏锚（voice anchor）：从人写参考文本切确定性片段，随 polish 视图下发。

参考片段供文风编辑观察叙述推进、停顿和信息密度；它不是句长目标，也不能
替代所选文风手册或本书人物的表达方式。

片段按章号种子确定性选取——同一章重建 pack 得到同一批切片（保 inputs_fingerprint
语义与 prompt cache），不同章滚动覆盖参考语料的不同段落。

依赖单向：本模块只做切片（纯函数），文件读取由调用方（pack.py）完成后传入，
与 voice_pack「装配层不碰磁盘」的边界一致。**内容防泄漏**由切片头部声明收口：
只观察节奏与语感，严禁借用其中人名/组织/设定/情节。
"""

from __future__ import annotations

import random
import re
from pathlib import Path

# 章节标题行（第X章/节/回 + 标题）与空行在切片前剥掉，只留正文段落。
_TITLE_RE = re.compile(
    r"^\s*(第[0-9零一二三四五六七八九十百千万]+[章节回][^\n]*|（\d+）|作者：.*|正文[^\n]*)$",
    flags=re.M,
)

ANCHOR_HEADER = (
    "【voice_anchor_text 人写节奏锚】下面是从人写参考作品切出的实锚片段。"
    "观察叙述如何推进、停顿和分配信息，不照搬句式或追求相同句长比例。"
    "**严禁借用其中任何人名/地名/组织/器物/设定/情节**——那是另一本书的血肉，不是你的素材。"
)


def load_anchor_source(path: Path) -> str:
    """读参考文本并做切片前清洗：编码探测（utf-8→gb18030）、剥标题行、压空行。"""
    raw = path.read_bytes()
    text: str | None = None
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("gb18030", errors="ignore")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TITLE_RE.sub("", text)
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return "\n".join(paras)


def select_anchor_slices(
    source: str,
    *,
    chapter: int,
    chars: int | None = None,
    slices: int = 2,
    focus: str = "",
    paragraph_ids: list[int] | None = None,
) -> str:
    """Select complete paragraphs by scene terms or explicit paragraph indexes.

    ``chars`` is accepted for compatibility and never clips a selected paragraph.
    Without matching scene terms, chapter-seeded sampling provides stable style
    examples without injecting the entire reference work into every chapter.
    """
    paras = [paragraph for paragraph in source.split("\n") if paragraph.strip()]
    if not paras:
        return ""
    rng = random.Random(f"voice-anchor:{chapter}")
    selected = list(dict.fromkeys(index for index in paragraph_ids or []
                                 if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(paras)))
    if not selected:
        terms = set()
        for run in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+", focus.lower()):
            if re.match(r"[\u3400-\u9fff]", run):
                terms.update(run[index:index + 2] for index in range(len(run) - 1))
            elif len(run) >= 3:
                terms.add(run)
        ranked = [(sum(term in paragraph.lower() for term in terms), rng.random(), index)
                  for index, paragraph in enumerate(paras)]
        related = [row for row in ranked if row[0] > 0]
        ranked = sorted(related or ranked, reverse=True)
        selected = [row[2] for row in ranked[:max(1, int(slices))]]
    chunks = [paras[index] for index in sorted(selected)]
    body = "\n……\n".join(chunks)
    return f"{ANCHOR_HEADER}\n\n{body}"


def build_voice_anchor(project_root: Path, cfg: dict, chapter: int, *, focus: str = "") -> str:
    """装配入口：读 config 的锚设置，返回可直接进 pack 的锚文本；未配置返回空串。

    `voice_anchor_file` 相对路径按**书项目根**（store.project，即含 book/ 的目录）解析，
    绝对路径原样使用。文件缺失不炸流水线——返回空串并交由上层记录提示。
    """
    file_ref = str(cfg.get("voice_anchor_file") or "").strip()
    if not file_ref:
        return ""
    path = Path(file_ref)
    if not path.is_absolute():
        path = project_root / file_ref
    if (project_root / "book" / "novel.sqlite3").is_file():
        from ..infra.artifact_path import ArtifactPath
        try:
            path = ArtifactPath(project_root / "book" / path.resolve().relative_to(project_root / "book"))
        except ValueError:
            return ""
    if not path.is_file():
        return ""
    source = load_anchor_source(path)
    if not source:
        return ""
    slices = int(cfg.get("voice_anchor_slices") or 2)
    return select_anchor_slices(source, chapter=chapter, slices=slices,
                                focus=str(cfg.get("voice_anchor_query") or focus),
                                paragraph_ids=cfg.get("voice_anchor_paragraphs"))
