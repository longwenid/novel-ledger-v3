"""Deterministic hard-setting card extraction from a source directory."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from ..infra.util import LedgerError, atomic_json, ok

EXCERPT_MAX_DEFAULT = 0

SCAN_SUFFIXES = {".md", ".markdown", ".yaml", ".yml", ".json"}

SKIP_DIR_NAMES = {
    ".git",
    ".cursor",
    ".claude",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "_归档",
    "venv",
    ".venv",
}

SKIP_FILE_NAMES = {
    "readme.md",
    "_模板.md",
    "license.md",
    "changelog.md",
    "kb_adapter.yaml",
    "kb_adapter.yml",
}

ROSTER_PATH_HINTS = (
    "人物名录",
    "角色名录",
    "人名录",
    "人物",
    "character",
    "characters",
    "名录",
)

ROSTER_TITLE_HINTS = ("人物名录", "角色名录", "人名录", "角色列表", "人物列表")

KIND_TITLE_RE = re.compile(
    r"(世界观|世界结构|世界边界|世界|宇宙|力量|修炼|境界|功法|灵根|神魂|"
    r"灵石|丹药|法宝|符箓|阵法|御器|储物|夺舍|天劫|雷劫|"
    r"武学|武功|内功|真气|经脉|异能|超能力|魔法|法术|斗气|血脉|灵能|机甲|义体|力量体系|"
    r"地理|地点|势力分布|气候|规则|法则|禁则|红线|秩序|定义)"
)

DIR_KIND_MAP = {
    "世界结构": "world",
    "geography": "geography",
    "world": "world",
    "power_system": "power_system",
    "rule": "rule",
}

KIND_FROM_TITLE = (
    ("地理", "geography"),
    ("地点", "geography"),
    ("势力分布", "geography"),
    ("气候", "geography"),
    ("力量", "power_system"),
    ("修炼", "power_system"),
    ("境界", "power_system"),
    ("功法", "power_system"),
    ("灵根", "power_system"),
    ("神魂", "power_system"),
    ("灵石", "power_system"),
    ("丹药", "power_system"),
    ("法宝", "power_system"),
    ("符箓", "power_system"),
    ("阵法", "power_system"),
    ("御器", "power_system"),
    ("储物", "power_system"),
    ("夺舍", "power_system"),
    ("天劫", "power_system"),
    ("雷劫", "power_system"),
    ("武学", "power_system"),
    ("武功", "power_system"),
    ("内功", "power_system"),
    ("外功", "power_system"),
    ("真气", "power_system"),
    ("经脉", "power_system"),
    ("招式", "power_system"),
    ("异能", "power_system"),
    ("超能力", "power_system"),
    ("超凡", "power_system"),
    ("魔法", "power_system"),
    ("法术", "power_system"),
    ("斗气", "power_system"),
    ("血脉", "power_system"),
    ("灵能", "power_system"),
    ("机甲", "power_system"),
    ("义体", "power_system"),
    ("力量体系", "power_system"),
    ("规则", "rule"),
    ("法则", "rule"),
    ("禁则", "rule"),
    ("红线", "rule"),
    ("秩序", "rule"),
    ("命名", "rule"),
    ("世界观", "world"),
    ("世界结构", "world"),
    ("世界边界", "world"),
    ("宇宙", "world"),
    ("世界", "world"),
    ("定义", "world"),
)

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
HARD_TOKEN_RE = re.compile(r"【硬】")
SOFT_HEADING_RE = re.compile(r"【软】")
SKIP_HEADING_RE = re.compile(
    r"(叙事层|关联设定|可写场景|钩子|对角色|情节的直接后果|参考资料)"
)
DEFAULT_SOFT_RE = re.compile(r"本条默认【软】")
HARDNESS_HARD_RE = re.compile(r"(硬度[*：:\s]*硬|hardness\s*[:=]\s*hard|按\**硬设定)")
YAML_HARDNESS_RE = re.compile(
    r"^(hardness|硬度)\s*:\s*(hard|硬)\s*$", re.I | re.M
)
SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def canon_source_fingerprint(source: Path) -> str:
    """正典**源目录**指纹：全部被扫描文件的相对路径 + 内容哈希。

    与 `store.canon_fingerprint()` 的区别是这条看的**不是**编译产物 `cards.json`，
    而是人写的 `book/kb/canon/**`。把它在 `kb sync` 时记进 cards.json，
    才能发现"改了正典却忘了 sync"——那种情况下运行时一直在用旧设定，
    而 `canon_fingerprint()` 因为读的是没变的 cards.json，一个字都不会变、
    也不会有任何 `canon_drift`。
    """
    from ..infra.util import sha256_text

    root = source if isinstance(source, Path) else Path(source)
    if not root.exists() or not root.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name.lower() in SKIP_FILE_NAMES:
            continue
        rel = path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"{rel}|{sha256_text(body)}")
    return "sha256:" + sha256_text("\n".join(parts))


_BAN_LINE = re.compile(r"^\s*[-*]\s*(.+)$")
_QUOTED_TERM = re.compile(r"[「『]([^」』]{2,16})[」』]")
_BOLD_TERM = re.compile(r"\*\*([^*]{2,16})\*\*")


def propose_glossary_candidates(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """从正典的「硬禁则」条目派生 glossary **候选**，供总编裁决后落 config。

    与 `propose_quant_keys` 同一思路：把"空白页"变成"候选清单"，消除"忘了填"这一环。
    glossary 的键是**禁用写法 → 规范写法**，而正典里写的是"勿写〈某种说法〉"这类**禁令**，
    两者不是一回事——所以这里**只做候选提取，绝不自动写入**：
    机器给出被正典点名过、且写成了具体词串（`**加粗**` / `「引号」`）的条目，
    由总编判断哪些够长、够限定语境，值得钉成禁用串（见 intent 模板 §五 的告警：
    `验阵抽两成` 可禁，通用 `抽两成` 不可禁）。
    """
    bullets: list[dict[str, str]] = []
    terms: list[str] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        body = str(card.get("body") or "")
        if "硬禁" not in body and "勿写" not in body and "勿把" not in body and "禁用" not in body:
            continue
        ident = str(card.get("id") or "")
        for line in body.splitlines():
            m = _BAN_LINE.match(line)
            if not m:
                continue
            text = m.group(1).strip()
            if not text or ("勿" not in text and "禁" not in text):
                continue
            if len(bullets) < 40:
                bullets.append({"card": ident, "bullet": text})
            for pat in (_QUOTED_TERM, _BOLD_TERM):
                for term in pat.findall(text):
                    term = term.strip()
                    if term and term not in terms:
                        terms.append(term)
    return {
        "ban_bullets": bullets,
        "named_terms": terms,
        "candidates": terms,
        "hint": (
            "候选来自正典「硬禁则」里被点名成具体词串的条目。glossary 的键是**禁用旧写法**，"
            "值才是规范写法：只把够长、能限定语境的串钉进去（正例 `验阵抽两成`）；"
            "通用短词（`抽两成`）会误伤真值，不要钉。写进 `config.glossary` 后，"
            "`chapter submit` / `chapter patch` / `book audit` 三处会自动拦。"
        ),
    }


def extract_to_file(
    source: Path,
    out: Path,
    *,
    excerpt_max: int = EXCERPT_MAX_DEFAULT,
) -> dict[str, Any]:
    result = extract_cards(source, excerpt_max=excerpt_max)
    if int(result.get("files_scanned") or 0) <= 0:
        raise LedgerError(
            "empty_kb_source",
            f"kb source has no supported canon files: {Path(source)}",
        )
    if not result.get("cards"):
        raise LedgerError(
            "empty_kb",
            f"kb source produced zero hard-setting cards; refusing to replace output: {Path(source)}",
            {"files_scanned": result.get("files_scanned")},
        )
    source_sha = canon_source_fingerprint(source)
    # `source_fingerprint` 是"这份 cards 由哪一版正典编译而来"的凭证：
    # `book audit` 拿它与当前 canon 源比对，报"改了正典却没 sync"。
    atomic_json(out, {"cards": result["cards"], "source_fingerprint": source_sha})
    return ok(
        out=str(out.resolve()),
        cards=len(result["cards"]),
        files_scanned=result["files_scanned"],
        skipped_roster=result["skipped_roster"],
        excerpt_max=excerpt_max,
        source_fingerprint=source_sha,
    )


def extract_cards(source: Path, *, excerpt_max: int = EXCERPT_MAX_DEFAULT) -> dict[str, Any]:
    root = source if isinstance(source, Path) else Path(source)
    if not root.exists():
        raise LedgerError("missing_source", f"kb source directory does not exist: {root}")
    if not root.is_dir():
        raise LedgerError("invalid_source", f"kb source is not a directory: {root}")
    # Retain the legacy argument for callers; compiled selected sections stay whole.
    cards: list[dict[str, Any]] = []
    skipped_roster = 0
    files_scanned = 0
    seen_ids: set[str] = set()

    for path in _iter_source_files(root):
        files_scanned += 1
        rel = _rel(root, path)
        if _is_roster(rel, path.stem):
            skipped_roster += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerError(
                "invalid_kb_source",
                f"cannot read canon source as UTF-8: {rel}",
                {"source": rel, "error": str(exc)},
            ) from exc
        suffix = path.suffix.lower()
        if suffix in {".yaml", ".yml", ".json"}:
            extracted = _from_structured(text, suffix, rel, excerpt_max)
        else:
            extracted = _from_markdown(text, rel, excerpt_max)
        for card in extracted:
            if _is_roster("", str(card.get("title") or "")):
                skipped_roster += 1
                continue
            card["id"] = _unique_id(str(card["id"]), seen_ids)
            cards.append(card)

    return {
        "cards": cards,
        "files_scanned": files_scanned,
        "skipped_roster": skipped_roster,
    }


def _iter_source_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name.lower() in SKIP_FILE_NAMES:
            continue
        found.append(path)
    found.sort(key=lambda p: str(p).replace("\\", "/"))
    return found


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _is_roster(rel: str, title: str) -> bool:
    blob = f"{rel} {title}".lower()
    for hint in ROSTER_TITLE_HINTS:
        if hint.lower() in blob:
            return True
    parts = rel.replace("\\", "/").split("/")
    for part in parts[:-1]:
        pl = part.lower()
        if pl in {h.lower() for h in ROSTER_PATH_HINTS}:
            return True
        if "名录" in part:
            return True
    return False


def _from_structured(text: str, suffix: str, rel: str, excerpt_max: int) -> list[dict[str, Any]]:
    try:
        if suffix == ".json":
            data = json.loads(text)
        else:
            data = _parse_simple_yaml(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LedgerError(
            "invalid_kb_source",
            f"cannot parse structured canon source: {rel}",
            {"source": rel, "error": str(exc)},
        ) from exc
    raw_cards: list[Any] = []
    if isinstance(data, dict) and isinstance(data.get("cards"), list):
        raw_cards = data["cards"]
    elif isinstance(data, list):
        raw_cards = data
    elif isinstance(data, dict) and (data.get("body") or data.get("title")):
        raw_cards = [data]
    else:
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw_cards):
        if not isinstance(item, dict):
            raise LedgerError(
                "invalid_kb_source",
                f"structured canon card[{i}] must be an object: {rel}",
                {"source": rel, "index": i},
            )
        for field in ("tags", "aliases"):
            value = item.get(field)
            if value is not None and not isinstance(value, list):
                raise LedgerError(
                    "invalid_kb_source",
                    f"structured canon card[{i}].{field} must be a list: {rel}",
                    {"source": rel, "index": i, "field": field},
                )
            if isinstance(value, list) and any(
                not isinstance(entry, str) or not entry.strip() for entry in value
            ):
                raise LedgerError(
                    "invalid_kb_source",
                    f"structured canon card[{i}].{field} must contain only non-empty strings: {rel}",
                    {"source": rel, "index": i, "field": field},
                )
        if _is_roster("", str(item.get("title") or item.get("id") or "")):
            continue
        kind = str(item.get("kind") or "")
        if kind in {"character", "name", "naming", "roster"}:
            continue
        hardness = str(item.get("hardness") or item.get("硬度") or "").lower()
        title = str(item.get("title") or item.get("id") or Path(rel).stem)
        body = str(item.get("body") or item.get("excerpt") or item.get("text") or "")
        prefer = hardness in {"hard", "硬"} or bool(KIND_TITLE_RE.search(title)) or kind in {
            "world",
            "geography",
            "power_system",
            "rule",
            "timeline_rule",
        }
        if not prefer:
            continue
        if not body.strip():
            continue
        out.append(
            _card(
                rel=rel,
                title=title,
                body=body,
                kind=kind or _kind_from_title(title, rel),
                excerpt_max=excerpt_max,
                index=i,
                extra_tags=[t for t in (item.get("tags") or []) if str(t).strip()],
                extra_aliases=[t for t in (item.get("aliases") or []) if str(t).strip()],
                always=item.get("always") is True,
                priority=str(item.get("priority") or "").strip(),
            )
        )
    return out


def _parse_simple_yaml(text: str) -> Any:
    """Minimal YAML object/list reader for hardness cards; not a full YAML engine."""
    stripped = text.strip()
    if not stripped:
        return {}
    # Adapter / mapping files are not knowledge cards.
    if re.search(r"^(version|dir_kind_map|stem_kind_overrides)\s*:", stripped, re.M):
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    data: dict[str, Any] = {}
    key = None
    multiline = False
    buf: list[str] = []
    for line in text.splitlines():
        if multiline:
            if line.startswith("  ") or line.startswith("\t") or not line.strip():
                buf.append(line[2:] if line.startswith("  ") else line)
                continue
            data[key] = "\n".join(buf).strip()
            multiline = False
            buf = []
        m = re.match(r"^([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val in {"|", ">"}:
            multiline = True
            buf = []
            continue
        data[key] = val.strip().strip('"').strip("'")
    if multiline and key is not None:
        data[key] = "\n".join(buf).strip()
    return data


def _from_markdown(text: str, rel: str, excerpt_max: int) -> list[dict[str, Any]]:
    file_hard = bool(HARDNESS_HARD_RE.search(text))
    kind_default = _kind_from_title(Path(rel).stem, rel)
    sections = _split_sections(text)
    cards: list[dict[str, Any]] = []
    for i, (heading, body) in enumerate(sections):
        if SOFT_HEADING_RE.search(heading) or SKIP_HEADING_RE.search(heading):
            continue
        title = heading or Path(rel).stem
        heading_hard = bool(HARD_TOKEN_RE.search(heading or ""))
        default_soft = _is_default_soft(heading, body)
        hard_units = _hard_units(body)
        kind = _kind_from_title(title, rel) or kind_default
        if heading_hard:
            cleaned = _strip_soft_marks(body)
            if not cleaned.strip():
                continue
            cards.append(
                _card(
                    rel=rel,
                    title=title,
                    body=cleaned,
                    kind=kind,
                    excerpt_max=excerpt_max,
                    index=i,
                )
            )
            continue
        if default_soft:
            if not hard_units:
                continue
            cards.append(
                _card(
                    rel=rel,
                    title=title,
                    body="\n".join(hard_units),
                    kind=kind,
                    excerpt_max=excerpt_max,
                    index=i,
                )
            )
            continue
        if hard_units:
            cards.append(
                _card(
                    rel=rel,
                    title=title,
                    body="\n".join(hard_units),
                    kind=kind,
                    excerpt_max=excerpt_max,
                    index=i,
                )
            )
            continue
        title_hit = bool(KIND_TITLE_RE.search(heading or title))
        if title_hit or (file_hard and heading and heading != Path(rel).stem):
            cleaned = _strip_soft_marks(body)
            if not cleaned.strip():
                continue
            cards.append(
                _card(
                    rel=rel,
                    title=title,
                    body=cleaned,
                    kind=kind,
                    excerpt_max=excerpt_max,
                    index=i,
                )
            )
    if cards:
        return cards
    # Fallback selects complete hard paragraphs or the first relevant section,
    # rather than clipping a fixed prefix of the entire source file.
    hard_units = _hard_units(text)
    if hard_units:
        return [
            _card(
                rel=rel,
                title=Path(rel).stem,
                body="\n".join(hard_units),
                kind=kind_default,
                excerpt_max=excerpt_max,
                index=0,
            )
        ]
    if file_hard or kind_default != "world" or KIND_TITLE_RE.search(Path(rel).stem):
        cleaned = _strip_soft(_first_section_body(text))
        if cleaned.strip():
            return [
                _card(
                    rel=rel,
                    title=Path(rel).stem,
                    body=cleaned,
                    kind=kind_default,
                    excerpt_max=excerpt_max,
                    index=0,
                )
            ]
    return []


def _split_sections(text: str) -> list[tuple[str, str]]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("", text.strip())]
    sections: list[tuple[str, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for i, match in enumerate(matches):
        title = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((title, body))
    return sections


def _is_default_soft(heading: str, body: str) -> bool:
    blob = f"{heading or ''}\n{body or ''}"
    return bool(DEFAULT_SOFT_RE.search(blob))


def _strip_soft_marks(text: str) -> str:
    stripped = _strip_soft(text)
    kept: list[str] = []
    for block in re.split(r"\n\s*\n", stripped):
        block = block.strip()
        if not block:
            continue
        lines = [ln for ln in block.splitlines() if not SOFT_HEADING_RE.search(ln)]
        if lines:
            kept.append("\n".join(lines))
    return "\n\n".join(kept).strip()


def _hard_units(text: str) -> list[str]:
    units: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or SOFT_HEADING_RE.search(block[:40]):
            continue
        if HARD_TOKEN_RE.search(block) or YAML_HARDNESS_RE.search(block):
            units.append(block)
            continue
        kept_lines = [ln for ln in block.splitlines() if HARD_TOKEN_RE.search(ln)]
        if kept_lines:
            units.append("\n".join(kept_lines))
    return units


def _strip_soft(text: str) -> str:
    parts = HEADING_RE.split(text)
    # HEADING_RE.split → [pre, hashes, title, body, hashes, title, body, ...]
    if len(parts) == 1:
        return text
    out = [parts[0]]
    for i in range(1, len(parts), 3):
        hashes, title, body = parts[i], parts[i + 1], parts[i + 2] if i + 2 < len(parts) else ""
        if SOFT_HEADING_RE.search(title):
            continue
        out.append(f"{hashes} {title}\n{body}")
    return "\n".join(out).strip()


def _first_section_body(text: str) -> str:
    sections = _split_sections(text)
    for heading, body in sections:
        if heading and SOFT_HEADING_RE.search(heading):
            continue
        if body.strip():
            return body
    return text


def _kind_from_title(title: str, rel: str) -> str:
    for hint, kind in KIND_FROM_TITLE:
        if hint in title:
            return kind
    for part in Path(rel).parts:
        if part in DIR_KIND_MAP:
            return DIR_KIND_MAP[part]
    return "world"


def _nfc_stem(rel: str) -> str:
    """文件名 stem 的 NFC 归一。

    背景包的正典文件是中文名（`境界.md` / `灵石.md` / `仙凡有别.md`），stem 参与下面的核心卡
    规则判定与 needle 检索。macOS 的 HFS+/APFS 按 NFD 分解存文件名，Linux 与 git 里通常是 NFC，
    同一个「境界」在两处的码点序列不同，直接 `==` 比较会在换平台后静默失配——`always` 加分丢了、
    中文 needle 检索少一路命中，而且不报错。
    """
    return unicodedata.normalize("NFC", Path(rel).stem)


def _core_card_flags(rel: str, title: str) -> dict[str, Any]:
    stem = _nfc_stem(rel)
    if stem == "境界" and ("寿元" in title or "分境" in title):
        return {"always": True}
    if stem == "灵石" and "品阶" in title:
        return {"always": True}
    if stem == "仙凡有别" and "总因" in title:
        return {"always": True}
    return {}


def _card(
    *,
    rel: str,
    title: str,
    body: str,
    kind: str,
    excerpt_max: int,
    index: int,
    extra_tags: list[Any] | None = None,
    extra_aliases: list[Any] | None = None,
    always: bool = False,
    priority: str = "",
) -> dict[str, Any]:
    excerpt = _clip(body.strip(), excerpt_max)
    tags = _tags_from(rel, title, kind)
    for t in extra_tags or []:
        s = str(t).strip()
        if s and s not in tags:
            tags.append(s)
    card = {
        "id": _id_for(rel, title, index),
        "kind": kind or "world",
        "title": title.strip() or Path(rel).stem,
        "body": excerpt,
        "tags": tags,
        "hardness": "hard",
        "source": rel,
    }
    aliases = [str(item).strip() for item in (extra_aliases or []) if str(item).strip()]
    if aliases:
        card["aliases"] = list(dict.fromkeys(aliases))
    if always:
        card["always"] = True
    if priority:
        card["priority"] = priority
    card.update(_core_card_flags(rel, title))
    return card


def _clip(text: str, excerpt_max: int) -> str:
    """Legacy name retained; preserve the complete selected source section."""
    return text.strip()


def _tags_from(rel: str, title: str, kind: str) -> list[str]:
    tags: list[str] = []
    for part in Path(rel).parts[:-1]:
        if part and part not in SKIP_DIR_NAMES and part not in tags:
            tags.append(part)
    if kind and kind not in tags:
        tags.append(kind)
    if "硬" not in tags:
        tags.append("硬")
    stem = _nfc_stem(rel)
    if stem and stem not in tags:
        tags.append(stem)
    _ = title
    return tags


def _id_for(rel: str, title: str, index: int) -> str:
    # 这里**故意不做** NFC 归一：stem 进的是卡片 id，归一会改掉已有项目 cards.json 里的
    # id，pack 的 needle 命中、always 核心卡引用都跟着变。跨平台（macOS NFD vs Linux NFC）
    # 失配只影响 id 字符串本身，代价远小于给所有已有项目换 id。
    stem = Path(rel).stem
    slug = SLUG_RE.sub("-", title).strip("-")[:40]
    if slug and slug != stem:
        return f"{stem}--{slug}"
    return f"{stem}--{index}"


def _unique_id(base: str, seen: set[str]) -> str:
    candidate = base or "card"
    n = 2
    while candidate in seen:
        candidate = f"{base}-{n}"
        n += 1
    seen.add(candidate)
    return candidate
