from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..infra.util import LedgerError, atomic_json, now_ts, read_json
from ..infra.scale import chapter_word_target_for_band, derive_chapter_word_target, scale_contract_errors
from ..infra.story_map import PLAN_SCHEMA, plan_shape_reject, plan_story_map_errors
from ..infra.volume_outline import VOLUME_OUTLINE_SCHEMA, VOLUME_OUTLINE_SIGNED_FIELDS, volume_outline_errors

try:  # POSIX（含 macOS）有 fcntl
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None  # type: ignore[assignment]

try:  # Windows 用 msvcrt 做字节区间锁
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 平台
    msvcrt = None  # type: ignore[assignment]

# Windows 的 msvcrt 是字节区间锁（等同 flock 的排他锁），但锁住第 0 字节会连带锁住
# LOCK 文件本身的读取——而拒绝路径要读持有者 JSON、测试也要读。所以把锁打到文件内容
# 之外很远的一处（1 MiB），既保留互斥，又不妨碍读写持有者信息。
_WIN_LOCK_OFFSET = 1 << 20

SCHEMA_VERSION = 1
CHARACTER_PROFILE_FIELDS = frozenset({
    "background_register", "speech_habits", "under_pressure",
    "desire_and_mask", "sample_quote",
})
KNOWLEDGE_REFS_CAP = 8


def validate_knowledge_refs(raw: Any, *, chapter: int = 0) -> list[str]:
    if raw is None:
        return []
    if (
        not isinstance(raw, list)
        or len(raw) > KNOWLEDGE_REFS_CAP
        or any(not isinstance(item, str) or not item.strip() for item in raw)
    ):
        raise LedgerError(
            "invalid_plan",
            f"chapter {chapter} knowledge_refs must be up to {KNOWLEDGE_REFS_CAP} non-empty topic ids",
        )
    refs = [item.strip() for item in raw]
    if len(set(refs)) != len(refs):
        raise LedgerError("invalid_plan", f"chapter {chapter} knowledge_refs must be unique")
    return refs


def validate_kb_cards(cards: Any) -> list[dict[str, Any]]:
    """Validate the compiled KB at every read/write boundary.

    A malformed card must fail here with a domain error, not later inside pack
    retrieval as an AttributeError.  Empty lists remain valid for projects that
    intentionally start without a canon; ``kb sync`` applies a stricter non-empty
    rule before replacing a compiled KB.
    """
    if not isinstance(cards, list):
        raise LedgerError("invalid_kb", "kb cards must be a list")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            raise LedgerError(
                "invalid_kb",
                f"kb card[{index}] must be an object",
                {"index": index, "type": type(card).__name__},
            )
        raw_ident = card.get("id")
        if not isinstance(raw_ident, str) or not raw_ident.strip():
            raise LedgerError(
                "invalid_kb",
                f"kb card[{index}] requires a non-empty string id",
                {"index": index},
            )
        ident = raw_ident.strip()
        if ident in seen:
            raise LedgerError("invalid_kb", f"duplicate kb card id: {ident}", {"index": index, "id": ident})
        raw_body = card.get("body") or card.get("excerpt")
        if not isinstance(raw_body, str) or not raw_body.strip():
            raise LedgerError(
                "invalid_kb",
                f"kb card[{index}] requires non-empty body or excerpt",
                {"index": index, "id": ident},
            )
        for field in ("tags", "aliases"):
            value = card.get(field)
            if value is not None and not isinstance(value, list):
                raise LedgerError(
                    "invalid_kb",
                    f"kb card[{index}].{field} must be a list",
                    {"index": index, "id": ident, "field": field},
                )
            if isinstance(value, list) and any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise LedgerError(
                    "invalid_kb",
                    f"kb card[{index}].{field} must contain only non-empty strings",
                    {"index": index, "id": ident, "field": field},
                )
        seen.add(ident)
        validated.append(card)
    return validated

def config_schema_version(cfg: dict[str, Any]) -> int:
    """Read the exact schema version; old or malformed projects are unsupported."""
    raw = cfg.get("schema_version")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LedgerError(
            "invalid_config",
            "config.schema_version must be an integer",
            {"schema_version": raw},
        )
    return raw


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_TENS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_ZERO = "零"


def _chinese_numeral_to_int(text: str) -> int | None:
    """把“第一卷/第一百零三章”这类小数字段解析成 int；解析不了返回 None。

    `零` 是占位符：跳过即可，前后单位已经决定了量级（一百零三 = 100 + 3）。
    口径只覆盖卷/章号这种小数字段，不做"壹佰贰拾叁"大写与"两万三千五"省略位解析。
    """
    if not text:
        return None
    total = 0
    section = 0
    for ch in text:
        if ch in _CN_TENS:
            section = section or 1
            total += section * _CN_TENS[ch]
            section = 0
        elif ch == _CN_ZERO:
            section = 0
        elif ch in _CN_NUM:
            section = _CN_NUM[ch]
        else:
            return None
    return total + section


def _pid_alive(pid: int) -> bool:
    """尽力判断持有锁的进程是否还活着。

    只在 POSIX 上用信号 0 探活：Windows 上 `os.kill` 对任意非 CTRL_* 信号会直接杀进程，
    不能拿来做探测。探测不到的平台一律返回 True——宁可停线等人工解锁，也不能把正在写的
    进程踢出锁。
    """
    if pid <= 0:
        return False
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _volume_key_number(raw: Any) -> int | None:
    """把卷键/卷取值归一成卷号（1 起），认不出返回 None。

    同一卷在盘上至少有三套写法：章拍 `volume`（1/"2"/"第二卷"）、`plan.volumes` 键
    （hatch 的 "vol-01"、目录名 "vol-0002"、或任意文本）、以及派生目录 vol-000N。
    卷键与目录名若互不匹配（如 "vol-01" vs `_volume_label(1)="vol-0001"`），
    `volume_meta` 会对全部章节返回 {}，卷脊/卷目标静默退化为全局卷脊，「正文目录、
    卷脊、记忆卷层」三方各说各话。归一成卷号比较是根治。
    """
    if isinstance(raw, bool) or raw is None:
        return 1
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if not text:
        return 1
    if text.isdigit():
        return int(text) if int(text) > 0 else None
    match = re.fullmatch(r"第\s*([0-9一二两三四五六七八九十百千万零]+)\s*卷", text)
    if match:
        body = match.group(1)
        if body.isdigit():
            return int(body)
        return _chinese_numeral_to_int(body)
    m = re.fullmatch(r"vol-0*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def volume_entry_for(volumes: Any, volume_key: Any) -> dict[str, Any] | None:
    """在 plan.volumes 里解析一卷：卷号归一优先，其次原始键与卷目录名两种旧写法。

    这是 volume_meta / outline 预算共用的唯一查卷入口；新增卷键写法只改这里。
    """
    if not isinstance(volumes, dict):
        return None
    number = _volume_key_number(volume_key)
    if number is not None:
        for key, item in volumes.items():
            if isinstance(item, dict) and _volume_key_number(key) == number:
                return item
    for key in (str(volume_key).strip(), _volume_label(volume_key)):
        item = volumes.get(key)
        if isinstance(item, dict):
            return item
    return None


def _volume_label(raw: Any) -> str:
    """返回卷文件夹名：数字/中文序号 → vol-0001；其他文本 → 清洗后 vol-<文本>。"""
    if isinstance(raw, bool) or raw is None:
        n = 1
    elif isinstance(raw, int):
        n = raw
    else:
        text = str(raw).strip()
        match = re.fullmatch(r"第\s*([0-9一二两三四五六七八九十百千万零]+)\s*卷", text)
        if match:
            body = match.group(1)
            if body.isdigit():
                n = int(body)
            else:
                parsed = _chinese_numeral_to_int(body)
                n = parsed if parsed is not None else 1
            return f"vol-{int(n):04d}"
        cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text).strip("-") or "1"
        return f"vol-{cleaned}"
    return f"vol-{max(1, int(n)):04d}"

PHASE_IDLE = "idle"
PHASE_AWAIT_DRAFT = "await_draft"
PHASE_AWAIT_POLISH = "await_polish"
PHASE_AWAIT_ASSEMBLY = "await_assembly"
PHASE_SUBMITTED = "submitted"
PHASE_AWAIT_ACK = "await_ack"
PHASE_BLOCKED = "blocked"
PHASE_COMPLETE = "complete"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    # token 数据由宿主逐请求主动回报；CLI 无法读取宿主账单。达到 stop 阈值后，下一次
    # 模型动作停在 usage_guard；确定性的 commit 仍可收口。阈值设为 0 可关闭对应告警/熔断。
    "usage_budget": {
        "warn_input_per_chapter": 300_000,
        "stop_input_per_chapter": 500_000,
    },
    # 流水线恒为 compact（draft→polish→assemble→ack，机器硬红线收口）。
    "require_ack": True,
    "ack_quotes_min": 3,
    "plan_low_water": 5,
    # 扩纲跨度：每次 extend_plan 必须覆盖到的章数（回执/简报以 extend_through_ch 显式下达，
    # worker 不再贴水位线补章）。整数 = 扩到 max_planned+N；"volume" = 按 plan.volumes 的
    # chapters_budget 一次签满当前卷（当前卷已签满则整签下一卷；无卷合同回退 20）。
    # 实测贴线补章：63 章烧了 13 轮扩纲 ≈109 万 token（占总盘 13%），每轮的钱≈写一章。
    "plan_extend_span": 20,
    "rewrite_limit": 1,
    "reopen_limit": 1,
    # style_check 机检未达标允许的返工次数：满额后 blocked（reason=style_metrics_failed），
    # 由人裁决，避免无人值守在“整章重写→重测”里空转。
    "style_metrics_limit": 2,
    # 润色收口的内容锚点机检（beats/must、连续性、glossary）未通过时允许的重润次数：
    # 满额停线 blocked（reason=polish_anchor_failed），由总编辑裁决——
    # 若是草稿本身就没兑现（锚点缺失源自 draft），从 blocked 走 retry-authorize 回 draft。
    "polish_anchor_limit": 2,
    # 节奏分布画像只在累计层比对：每 N 章 ack / book audit 时对已提交正文跑同一基线。
    "style_fingerprint_every": 10,
    "style_fingerprint_min": 5,
    "word_band": {"min": 2500, "max": 8000},
    "word_band_enforce": True,
    # 会话边界：strict = 每章 ack 后必须新开会话再写下一章（默认；防整书历史随请求重发）。
    # inline-compact = 允许同一会话继续下一章，条件是宿主先做会话内压缩（丢正文、只留账本指针）。
    # 用于既开不了新会话、本机又没有可无头调用的模型 CLI 时的连写场景。
    "session_mode": "strict",
    # 全书目标字数（新书启动设定，`init --book-words`）。默认 200 万；既用于进度，
    # 也用于计划耗尽、提前终局与 book complete 的规模门禁。
    "book_words": 2_000_000,
    # stage-agent = one empty-context model conversation per stage (default).
    # worker-agent/inline are explicit legacy responsibility-only modes.
    "execution_mode": "stage-agent",
    "review_contract_version": 2,
    # ack / 写者回灌的临时笔记窗口（`voice.json` 的 session_notes）。与蒸馏概念分开滑窗，
    # 只在 pack_caps.voice_concepts 的剩余槽里补位，永远挤不掉 voice apply 写的长驻概念。
    "voice_session_notes_cap": 0,  # legacy compatibility; persistent feedback is not evicted
    # auto = 市井烟火（shijing）自动启用人写节奏机检；false/off 关闭
    "style_check": "auto",
    # 默认只写作模式（用户裁决）：draft-submit 直通组装，草稿即终稿。
    # 字数带（章合同）仍在 draft-submit 收口；文风机检整体退出写链，要自查用
    # `chapter precheck`（只读）。按书恢复润色：`config set --key polish --value on`。
    "polish": "off",
    # 日志体开场策略：discourage=默认（简报带时间呈现纪律，机检点名连续日期句开场）；
    # allow=日记体/报告体书豁免。探因：简报灌满绝对日期+机检盯时间线，写者安全牌
    # 收敛成每章「X月X号早上X点」开场，连续数十章后读感从小说滑向工作日志。
    "dateline_openings": "discourage",
    "pack_caps": {
        "kb_slice": 8,
        "kb_excerpt_chars": 0,  # deprecated: selected context is never character-truncated
        "near_summaries": 3,
        "history_records": 8,
        "history_chars": 0,
        "debts": 8,
        "hooks": 5,
        "relations": 6,
        "state_pinned": 6,
        "state_recent": 8,
        # 场景地图（地点簿）切片上限：本章命中 + 最近使用补满。
        "locations": 8,
        # 概念数量随手册更新；cap 需给各文风的会话笔记留余量。
        "voice_concepts": 24,
        "now_card_chars": 0,
        "occupancy_per_location": 12,
        # 活跃不可逆状态超出此数时装配停线；可临时上调用于一章显式结案治理。
        "irreversible_conditions": 32,
        "story_focus_chars": 0,
    },
    "protagonist": "",
    # 跨章数字对账（口径项）。每项是一个**项目层**的口径名词（如「年租」「折价」）。
    # 填了它，book audit / book reconcile 会逐章检查同一键是否出现两个互斥金额
    # （same_key_conflict）；plan validate 会检查它是否进了 world_spine（每章 pack
    # 必注入的通道）。**只列"每章应当单一取值"的键**——「本利」「利钱」这类一章内
    # 本就可能有两笔不同债务的键不要列，否则产生假阳。留空则两项检查恒不触发，零误报。
    "quant_keys": [],
    # 术语归一化表（`{禁用旧写法: 规范写法}`）。submit / patch / book audit 三处都会读它，
    # 但只有项目显式填了才生效——开书时由作者按 intent 模板 §五 的「禁用写法」列填。
    # 默认给空表而不是不给键：键在配置里可见，才知道这个闸门存在、该往哪填。
    "glossary": {},
    # 书级事实登记表（`{键名: {kind, canonical, observe/suffixes, …}}`）。
    # 长篇最贵的缺陷是「同一件事在书内有两套说法」——同一个人换了称谓、同一个数字两个值、
    # 同一批数目对不上、同一年在三处三个说法。这类错单章读不出来，只有把**取值域**声明
    # 出来才能机检。判据与观测全在 `content/consistency.py`（题材无关），取值全部由本键声明：
    #   kind=entity  canonical 是规范称谓；`suffixes:["家"]` 按后缀族扫，或 `observe:"称谓"`
    #                按名字扫，`aliases` 列合法别名（身份揭示）。
    #   kind=number  canonical 是规范数值；`observe` 是键（如「岁」），`tolerance` 容差。
    #   kind=date    canonical 是规范年（公历）；`observe` 是键，`era_map` 给纪年基准/特例。
    #   kind=set     canonical 是首选取值，`allow` 列同处允许的其他成员（二选一互斥）。
    #   kind=count   canonical 是一批事物的总数；`observe` 是那批事物的名词。
    # 留空 → 全部事实探针恒空输出（默认零误报）。填法、常见失误与修复 SOP 见
    # `references/fact-registry.md`；候选由 `book calibrate` 从正典派生（只建议不自动写）。
    "fact_keys": {},
    # 引号体例：auto=只判「同一章内只准一种体系、配对必须闭合」；也可显式锁定
    # cn_double/cn_corner/zh_book/ascii。成稿里混用两三套引号是典型的多稿缝合残留，
    # 单看一章发现不了，靠 `book audit` 的体例汇总点名。
    "quote_style": "auto",
    # 跨章扫描的强度旋钮（advisory 级判据的阈值；误报高就抬高阈值或调小 cap）。
    "consistency_scan": {
        "near_duplicate_min_hans": 60,
        "near_duplicate_threshold": 0.85,
        "repeated_phrase_min_count": 6,
        "repeated_phrase_cap": 20,
    },
}

# 编拍纪律：每章 5 场戏。空列表仍 missing_beats；条数不在带内只警告，submit 不因此 rejected。
# 从 3–4 抬到 4–5：实测每场稳定落在 550–650 汉字、与指令无关，场数是唯一
# 可靠的结构杠杆；aim=3200 需要 5 场才首轮进带，3 场数学上必然吃"补整场戏"返工。
# 下限再抬 4→5（带收成 5–5）：生产书实测 planner 贴
# "4–5"下限走，4 场章 draft/polish 贴线欠交（2050–2498），7 章里 5 章吃一轮返工；
# aim 3200 ≈ 5 场，统一 5 场。
BEATS_SUGGEST_MIN = 5
BEATS_SUGGEST_MAX = 5
BEATS_SCENE_HINT = (
    "每章保证 5 个事件；每个事件是一场戏（当场发生、有来往、有未完），"
    "不是拍点标签或 must 词。字数是结果：偏短先加事件，不注水。"
    "限制视角下一场像样事件大约撑一段戏；实测每场稳定 550–650 汉字、与指令无关，"
    "4 场只有 2200–2500 字贴线欠交，5 场才稳；功能清单式五场仍然会短。"
)


def beats_count_warning(chapter: int, beats: list[Any]) -> dict[str, Any] | None:
    n = len(beats)
    if BEATS_SUGGEST_MIN <= n <= BEATS_SUGGEST_MAX:
        return None
    return {
        "code": "beats_count_off_band",
        "chapter": chapter,
        "beats": n,
        "suggest_min": BEATS_SUGGEST_MIN,
        "suggest_max": BEATS_SUGGEST_MAX,
        "hint": BEATS_SCENE_HINT,
    }


# 书级大纲（plan 顶层 `book_outline`，可选）：把 book_words 切成幕/卷的机检骨架。
# 叙事缺项软诊断；规模合同由 infra.scale 硬校验。
OUTLINE_MILESTONE_KINDS = ("volume_payoff", "turning_point", "book_climax")


def outline_budget_for_volume(volumes: Any, volume_key: Any) -> int | None:
    """读取某卷声明的 `word_budget`（卷号归一解析，兼容原始键与卷目录名两种写法），无则返回 None。"""
    item = volume_entry_for(volumes, volume_key)
    if item is not None and item.get("word_budget") is not None:
        try:
            return int(item["word_budget"])
        except (TypeError, ValueError):
            return None
    return None


class BookStore:
    def __init__(self, project: Path):
        self.project = project.resolve()
        from .artifact_path import ArtifactPath
        from .database import BookDatabase
        self.book = ArtifactPath(self.project / "book")
        self.database = BookDatabase(Path(str(self.book)))
        self.database_path = self.database.path
        self.config_path = self.book / "config.json"
        self.run_dir = self.book / "run"
        self.head_path = self.run_dir / "HEAD.json"
        self.lock_path = Path(str(self.run_dir / "LOCK"))
        self.quality_log_path = self.run_dir / "quality.jsonl"
        self.autopilot_state_path = self.run_dir / "autopilot.json"
        self.autopilot_events_path = self.run_dir / "autopilot-events.jsonl"
        self.ledger_dir = self.book / "ledger"
        self.events_path = self.ledger_dir / "events.jsonl"
        self.snapshot_path = self.ledger_dir / "snapshot.json"
        self.plan_dir = self.book / "plan"
        self.plan_path = self.plan_dir / "chapters.json"
        self.kb_dir = self.book / "kb"
        self.kb_path = self.kb_dir / "cards.json"
        self.canon_dir = self.kb_dir / "canon"
        self.pack_dir = self.book / "pack"
        self.current_pack_path = self.pack_dir / "current.json"
        self.staging_dir = self.book / "staging"
        self.output_path = ArtifactPath(self.staging_dir / "output.json")
        self.chapters_dir = self.book / "chapters"
        self.summaries_dir = self.book / "summaries" / "l1"
        self.acks_dir = self.book / "acks"
        self.memory_dir = self.book / "memory"
        self.voice_memory_path = self.memory_dir / "voice.json"
        self.hierarchical_memory_path = self.memory_dir / "hierarchy.json"
        self.usage_path = self.book / "usage.jsonl"
        self.editorial_dir = self.book / "editorial"
        self.intent_path = self.editorial_dir / "intent.md"
        self.outline_path = self.editorial_dir / "outline.md"
        self.decisions_path = self.editorial_dir / "decisions.jsonl"
        self.hatch_manifest_path = self.editorial_dir / "hatch-manifest.json"

    def ensure_layout(self) -> None:
        if not self.database.exists() and Path(str(self.head_path)).exists():
            raise LedgerError("database_migration_required", "migrate existing file project before initialization")
        for path in (
            self.book,
            self.run_dir,
            self.ledger_dir,
            self.plan_dir,
            self.kb_dir,
            self.canon_dir,
            self.pack_dir,
            self.staging_dir,
            self.chapters_dir,
            self.summaries_dir,
            self.acks_dir,
            self.memory_dir,
            self.editorial_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def transaction(self):
        if not self.database.exists() and (Path(str(self.head_path)).exists() or Path(str(self.config_path)).exists()):
            raise LedgerError("database_migration_required", "file project requires database migrate before writes")
        return self.database.transaction()

    @contextlib.contextmanager
    def exclusive_lock(self) -> Iterator[Path]:
        """所有写命令的进程间互斥锁（`book/run/LOCK`）。

        SKILL.md 鼓励"每章新开会话"，两个会话并发跑 chapter next 会互相覆盖
        HEAD/events/snapshot。拿不到锁的一方直接以 `locked` 失败，不做等待——
        长跑要的是"另一个会话在写，你别插队"，不是排队。

        POSIX 用 `flock`，Windows 用 `msvcrt` 字节区间锁；两者都在进程退出/句柄关闭时
        自动释放，所以崩溃不会留下死锁（这也是不用 O_EXCL 建锁文件的原因）。缺这两个
        原语的极罕见平台退化为原子 `mkdir` 目录锁（`book/run/LOCK.d`），而不是静默放行——
        静默放行等于把事件溯源的一致性押在"没人会并发"上。
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:
            with self._hold_directory_lock():
                yield self.lock_path
            return
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            self._lock_acquire(handle)
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps({"pid": os.getpid(), "ts": now_ts()}, ensure_ascii=False))
                handle.flush()
                yield self.lock_path
            finally:
                self._lock_release(handle)
        finally:
            handle.close()

    def _lock_acquire(self, handle: Any) -> None:
        """非阻塞获取排他锁；被别的写命令占用则抛 `locked`（含持有者信息）。

        调用前 `exclusive_lock` 已确保至少有一个锁原语可用（否则走目录锁分支）。
        """
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(_WIN_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.seek(0)
            holder = handle.read().strip()
            raise LedgerError(
                "locked",
                f"another novel-ledger write command holds the lock: {self.lock_path}",
                {"lock": str(self.lock_path), "holder": holder or None},
            ) from exc

    def _lock_release(self, handle: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(_WIN_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            # 释放失败不掩盖真正的结果：句柄关闭时操作系统会兜底释放。
            pass

    def _directory_lock_path(self) -> Path:
        return self.lock_path.with_name(self.lock_path.name + ".d")

    @staticmethod
    def _read_lock_owner(lock_dir: Path) -> Any:
        owner_path = lock_dir / "owner.json"
        if not owner_path.exists():
            return None
        try:
            return read_json(owner_path)
        except LedgerError:
            return {"note": f"unreadable owner file: {owner_path}"}

    @contextlib.contextmanager
    def _hold_directory_lock(self) -> Iterator[None]:
        """无 fcntl/msvcrt 平台的原子 mkdir 互斥。

        目录锁不会随进程退出自动释放，所以持锁信息写进 `owner.json`：
        只有**确认**持有者进程已死（POSIX 可探活）才回收；探测不到就保守停线，
        并在错误详情里给出人工解锁路径。宁可要一次人工介入，也不要两个写者同时进。
        """
        lock_dir = self._directory_lock_path()
        if not self._try_acquire_directory_lock(lock_dir):
            holder = self._read_lock_owner(lock_dir)
            raise LedgerError(
                "locked",
                f"another novel-ledger write command holds the lock: {lock_dir}",
                {
                    "lock": str(lock_dir),
                    "holder": holder,
                    "hint": f"if no writer is running, delete {lock_dir} and retry",
                },
            )
        try:
            yield
        finally:
            shutil.rmtree(lock_dir, ignore_errors=True)

    def _try_acquire_directory_lock(self, lock_dir: Path) -> bool:
        payload = {"pid": os.getpid(), "ts": now_ts()}
        for _ in range(2):
            try:
                lock_dir.mkdir()
            except FileExistsError:
                owner = self._read_lock_owner(lock_dir)
                pid = int((owner or {}).get("pid") or 0) if isinstance(owner, dict) else 0
                if pid and not _pid_alive(pid):
                    # 持有者已死（断电/崩溃留下残锁）：回收后重试一次
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
                return False
            try:
                atomic_json(lock_dir / "owner.json", payload)
            except Exception:
                # 写 owner 失败就必须把目录收掉：留一个没有 owner.json 的锁目录
                # 会让后续所有写命令都判"有人持锁"，而没人能自证清白。
                shutil.rmtree(lock_dir, ignore_errors=True)
                raise
            return True
        return False

    def load_config(self) -> dict[str, Any]:
        if not self.database.exists() and Path(str(self.config_path)).exists():
            raise LedgerError("database_migration_required", "file project requires database migrate before use")
        if not self.config_path.exists():
            raise LedgerError("not_initialized", f"missing config: {self.config_path}")
        cfg = read_json(self.config_path)
        found = config_schema_version(cfg)
        if found != SCHEMA_VERSION:
            raise LedgerError(
                "unsupported_schema",
                f"project schema_version {found} does not match this skill's {SCHEMA_VERSION}",
                {"found": found, "current": SCHEMA_VERSION},
            )
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        for section in ("word_band", "pack_caps", "usage_budget"):
            if section in cfg and isinstance(cfg[section], dict):
                base = copy.deepcopy(DEFAULT_CONFIG[section])
                base.update(cfg[section])
                merged[section] = base
        return merged

    # 无条件托管的前缀：这些位置磁盘上存在而 DB 没有的文件 = 宿主越层直写（对控制面不可见）。
    UNCONDITIONALLY_MANAGED_PREFIXES = (
        "plan/", "pack/", "editorial/", "kb/", "memory/", "summaries/", "ledger/",
    )

    def document_shadow_divergence(self, *, cap: int = 20) -> dict[str, Any] | None:
        """托管文档的投影漂移清单：磁盘被改（DB 有旧值）+ 越层直写（DB 侧为空）。

        ArtifactPath 架构下磁盘只是投影：宿主用 Write/裸 IO 直写托管文件对控制面
        不可见，问题会以「若干章后读旧值/作废整稿」的形态炸出（真实项目曾整夜
        误诊为缓存抖动）。status 携带本清单把漂移点名成证据与教学提示；干净返回 None。
        投影按需缺席（DB 有、磁盘无）不算漂移。
        """
        book = Path(str(self.book))
        diverged: list[str] = []
        checked = 0
        for key in self.database.paths():
            plain = book / key
            if not plain.exists():
                continue
            checked += 1
            if plain.read_bytes() != self.database.read(key):
                diverged.append(key)
                if len(diverged) >= cap:
                    break
        host_only: list[str] = []
        for prefix in self.UNCONDITIONALLY_MANAGED_PREFIXES:
            area = book / prefix.rstrip("/")
            if not area.is_dir():
                continue
            for plain in sorted(area.rglob("*")):
                if not plain.is_file():
                    continue
                key = plain.relative_to(book).as_posix()
                if key.startswith("novel.sqlite3"):
                    continue
                if not self.database.has(key):
                    host_only.append(key)
                    if len(host_only) >= cap:
                        break
        if not diverged and not host_only:
            return None
        return {
            "disk_edited": diverged,
            "host_only": host_only,
            "checked": checked,
            "hint": (
                "these files live in the sqlite database; the disk copies are projections. "
                "disk_edited = someone edited the projection directly (database keeps the old "
                "value); host_only = a file written outside the authoritative channel and thus "
                "invisible to the control plane. Re-apply both through the CLI / BookStore "
                "(save_plan, save_config, ArtifactPath writes) — never Write/raw IO on book/ "
                "managed files."
            ),
        }

    def config_shadow_divergence(self) -> dict[str, Any] | None:
        """检出「config.json 文件被手工改过、数据库真源未变」的漂移。

        数据库是配置唯一真源，磁盘 config.json 只是投影视图（exports_and_inputs），
        散改永远不生效且无任何提示——排查者会误以为改了没生效是缓存或读抖动。
        返回漂移证据与教学提示；一致或无库时返回 None。
        """
        plain = Path(str(self.config_path))
        if not self.database.exists() or not plain.exists() or not self.database.has("config.json"):
            return None
        file_body = plain.read_bytes()
        db_body = self.database.read("config.json")
        if file_body == db_body:
            return None

        def _tag(body: bytes) -> str:
            return "sha256:" + hashlib.sha256(body).hexdigest()[:16]

        return {
            "file": str(plain),
            "file_hash": _tag(file_body),
            "database_hash": _tag(db_body),
            "hint": (
                "config.json on disk is a projected view; the database is the source of "
                "truth, so direct file edits never take effect. Re-apply the intended "
                "change through `config set --key <key> --value <value>` (effective from "
                "the next command; no restart needed)."
            ),
        }

    def save_config(self, cfg: dict[str, Any]) -> None:
        cfg = dict(cfg)
        ref = str(cfg.get("voice_anchor_file") or "").strip()
        if ref:
            source = Path(ref)
            if not source.is_absolute():
                source = self.project / source
            target = self.memory_dir / "voice-anchor.txt"
            if source.resolve() != Path(str(target)).resolve() and source.is_file():
                target.write_bytes(source.read_bytes())
                cfg["voice_anchor_file"] = "book/memory/voice-anchor.txt"
        atomic_json(self.config_path, cfg)

    def read_head(self) -> dict[str, Any]:
        if not self.database.exists() and Path(str(self.head_path)).exists():
            raise LedgerError("database_migration_required", "file project requires database migrate before use")
        if not self.head_path.exists():
            raise LedgerError("not_initialized", f"missing HEAD: {self.head_path}")
        return read_json(self.head_path)

    def write_head(self, head: dict[str, Any]) -> None:
        atomic_json(self.head_path, head)

    def load_plan(self) -> dict[str, Any]:
        if not self.plan_path.exists():
            return {"title": "", "protagonist": "", "volume_spine": "", "chapters": []}
        data = read_json(self.plan_path)
        if not isinstance(data, dict):
            raise LedgerError("invalid_plan", "plan must be a JSON object")
        chapters = data.get("chapters")
        if chapters is None:
            chapters = []
        if not isinstance(chapters, list):
            raise LedgerError("invalid_plan", "plan.chapters must be a list")
        # 保留全部顶层字段（world_spine / brief / 自定义扩展键等），
        # 只对三个契约字段补默认值。曾只回传 4 个键，导致 `plan extend`
        # 把 world_spine 等顶层字段永久写丢。
        plan = dict(data)
        plan.setdefault("title", "")
        plan.setdefault("protagonist", "")
        plan.setdefault("volume_spine", "")
        plan["chapters"] = chapters
        return plan

    def save_plan(self, plan: dict[str, Any]) -> None:
        atomic_json(self.plan_path, plan)

    def extend_plan(
        self,
        new_chapters: list[dict[str, Any]],
        *,
        phase_brief: dict[str, Any] | None = None,
        volumes: dict[str, Any] | None = None,
        character_profiles: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """向章拍真源追加新章（只能向后追加，不能插中间），并可同批签出「卷合同」。

        空 beats 仍 `missing_beats` 硬拒。条数不为 5 只附软警告 `beats_count_off_band`，
        不拒写入——每章 5 场戏是编拍纪律，不是 submit 硬门禁。

        `volumes` 是可选的卷合同表：merge 语义（同键覆盖、不删除既有键），每键一个
        卷元数据 dict（title/spine/goal/word_budget/chapters_budget）。提供 word_budget
        与 chapters_budget 时受 scale 合同硬校验（按本书签约章幅折算）。
        允许只签卷或只更新人物短卡而不追加章；人物卡按规范名整条替换，保留其他人物。
        跨卷长跑开工前可先把后续卷的预算与卷脊签成机器可校验的事实。
        """
        if not isinstance(new_chapters, list):
            raise LedgerError("invalid_plan", "extend requires a chapters list")
        # effects 稳定键防线：缺键条目（如 facts 用 `fact` 而非 `text`）在组装提交时
        # 无论写成什么都无法对账，只能 blocked 停线。落盘前点名拒收，plan worker
        # 可按错误清单当场改键重交。
        plan_shape_reject(new_chapters, origin="plan extend")
        if volumes is not None:
            if not isinstance(volumes, dict) or not volumes:
                raise LedgerError("invalid_plan", "volumes must be a non-empty object when provided")
            for key, item in volumes.items():
                if not str(key or "").strip() or not isinstance(item, dict):
                    raise LedgerError(
                        "invalid_plan",
                        "each volumes entry must be a non-empty key mapped to an object",
                        {"volume": str(key)},
                    )
        if character_profiles is not None:
            if not isinstance(character_profiles, dict) or not character_profiles:
                raise LedgerError("invalid_plan", "character_profiles must be a non-empty object when provided")
            for name, record in character_profiles.items():
                if not isinstance(name, str) or not name.strip() or not isinstance(record, dict):
                    raise LedgerError("invalid_plan", "each character profile needs a non-empty name and object")
                unknown = set(record) - CHARACTER_PROFILE_FIELDS
                if unknown:
                    raise LedgerError("invalid_plan", f"unknown character profile fields for {name!r}: {sorted(unknown)}")
                for field, value in record.items():
                    if not isinstance(value, str):
                        raise LedgerError(
                            "invalid_plan",
                            f"character_profiles[{name!r}].{field} must be a string",
                        )
        if not new_chapters and not volumes and not character_profiles:
            raise LedgerError("invalid_plan", "extend requires chapters, volumes, or character_profiles")
        plan = self.load_plan()
        config = self.load_config()
        chapter_target = derive_chapter_word_target(plan, config) or chapter_word_target_for_band(config.get("word_band"))
        existing = plan.get("chapters") or []
        max_ch = max((int(i.get("chapter") or 0) for i in existing if int(i.get("chapter") or 0) > 0), default=0)
        added: list[int] = []
        seen: set[int] = set()
        warnings: list[dict[str, Any]] = []
        volume_errors: list[dict[str, Any]] = []
        if volumes:
            # 卷合同自身的硬校验：scale 合同只在 book_outline 存在时启用，而纯签卷恰恰
            # 可能发生在手排小项目上——卷预算声明了就必须自洽，与是否有 outline 无关。
            for key, item in volumes.items():
                try:
                    words_i = int(item["word_budget"]) if item.get("word_budget") is not None else None
                except (TypeError, ValueError):
                    words_i = None
                try:
                    count_i = int(item["chapters_budget"]) if item.get("chapters_budget") is not None else None
                except (TypeError, ValueError):
                    count_i = None
                if (words_i is not None and words_i <= 0) or (count_i is not None and count_i <= 0):
                    volume_errors.append({
                        "code": "book_scale_volume_budget_invalid",
                        "volume": str(key),
                        "word_budget": words_i,
                        "chapters_budget": count_i,
                        "message": "volume budgets must be positive integers when present",
                    })
                elif words_i is not None and count_i is not None:
                    expected_count = -(-words_i // chapter_target)
                    basis = (plan.get("book_outline") or {}).get("chapter_rebudget") or {}
                    if str(key) == str(basis.get("continuation_volume")):
                        expected_count += int(basis.get("additional_chapters") or 0)
                    if count_i == expected_count:
                        continue
                    volume_errors.append({
                        "code": "book_scale_volume_chapters_mismatch",
                        "volume": str(key),
                        "word_budget": words_i,
                        "chapters_budget": count_i,
                        "expected_chapters_budget": expected_count,
                        "chapter_words_target": chapter_target,
                        "message": f"volume chapters_budget must equal ceil(word_budget / {chapter_target})",
                    })
            if volume_errors:
                raise LedgerError(
                    "book_scale_contract_violation",
                    "signed volume contract is inconsistent",
                    {"errors": volume_errors},
                )
        # 先整体校验，通过后才写入，避免部分追加
        for item in new_chapters:
            if not isinstance(item, dict):
                raise LedgerError("invalid_plan", "each chapter must be an object")
            ch = int(item.get("chapter") or 0)
            if ch <= 0:
                raise LedgerError("invalid_plan", f"chapter number must be a positive integer: {item.get('chapter')!r}")
            validate_knowledge_refs(item.get("knowledge_refs"), chapter=ch)
            refs = item.get("memory_refs", [])
            if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(ref, str) or not ref.strip() for ref in refs) or len(set(refs)) != len(refs):
                raise LedgerError("invalid_plan", "memory_refs must be up to 16 unique non-empty event ids")
            query = item.get("memory_query", "")
            if not isinstance(query, str):
                raise LedgerError("invalid_plan", "memory_query must be a string")
            if ch in seen:
                raise LedgerError("duplicate_chapter", f"duplicate chapter number in extend list: {ch}")
            seen.add(ch)
            if ch <= max_ch:
                raise LedgerError(
                    "chapter_not_append",
                    f"new chapters must extend beyond the current max ({max_ch}); got {ch}",
                    {"chapter": ch, "max_chapter": max_ch},
                )
            beats = item.get("beats")
            if not isinstance(beats, list) or not beats:
                raise LedgerError("missing_beats", f"chapter {ch} extend requires a non-empty beats list")
            # 五场是适合默认章幅的编拍建议；短章或特殊结构可少排，只要非空。
            warn = beats_count_warning(ch, beats)
            if warn:
                warnings.append(warn)
            added.append(ch)
        chapters = list(existing) + [dict(i) for i in new_chapters]
        chapters.sort(key=lambda i: int(i.get("chapter") or 0))
        candidate = dict(plan)
        candidate["chapters"] = chapters
        merged_volumes = dict(plan.get("volumes") or {})
        if volumes:
            if plan.get("volume_outline_contract") is not None or config.get("volume_outline_schema") == VOLUME_OUTLINE_SCHEMA:
                for key, value in volumes.items():
                    if str(key) not in merged_volumes:
                        raise LedgerError("volume_outline_contract_violation", "plan extend may only refine an existing signed volume", {"volume": str(key)})
                    previous = merged_volumes[str(key)]
                    changed = [field for field in VOLUME_OUTLINE_SIGNED_FIELDS if field in value and value[field] != previous.get(field)]
                    if changed:
                        raise LedgerError("volume_outline_contract_violation", "plan extend cannot replace signed whole-book volume obligations", {"volume": str(key), "changed_fields": changed})
                    merged_volumes[str(key)] = {**previous, **dict(value)}
            else:
                merged_volumes.update({str(k): dict(v) for k, v in volumes.items()})
            candidate["volumes"] = merged_volumes
        if character_profiles:
            merged_profiles = dict(plan.get("character_profiles") or {})
            merged_profiles.update({name: dict(profile) for name, profile in character_profiles.items()})
            candidate["character_profiles"] = merged_profiles
        contract_required = bool(added) and (
            candidate.get("schema") == PLAN_SCHEMA or self.hatch_manifest_path.exists()
        )
        if contract_required:
            if not isinstance(phase_brief, dict):
                raise LedgerError(
                    "event_spine_phase_required",
                    "event-spine plan extension requires one signed phase brief",
                )
            expected_numbers = list(range(min(added), max(added) + 1))
            if sorted(added) != expected_numbers:
                raise LedgerError(
                    "event_spine_phase_gap",
                    "one phase expansion must append a contiguous chapter range",
                    {"chapters": sorted(added)},
                )
            if (
                phase_brief.get("chapter_start") != min(added)
                or phase_brief.get("chapter_end") != max(added)
            ):
                raise LedgerError(
                    "event_spine_phase_range_mismatch",
                    "phase brief range must exactly match the appended chapter batch",
                    {"added": sorted(added)},
                )
            phase_id = str(phase_brief.get("id") or "").strip()
            if not phase_id or any(str(item.get("phase_id") or "").strip() != phase_id for item in new_chapters):
                raise LedgerError(
                    "event_spine_phase_id_mismatch",
                    "every appended chapter must reference the supplied phase brief id",
                    {"phase_id": phase_id},
                )
            candidate["phases"] = [*(plan.get("phases") or []), dict(phase_brief)]
        from ..infra.planning import expansion_selection
        selection_events = [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines() if line.strip()] if self.events_path.exists() else []
        selection = expansion_selection(selection_events, new_chapters, required=contract_required and int(config.get("review_contract_version") or 1) >= 2)
        if selection:
            candidate["expansion_batches"] = [*(plan.get("expansion_batches") or []), selection]
        volume_outline_issues = volume_outline_errors(candidate, required=config.get("volume_outline_schema") == VOLUME_OUTLINE_SCHEMA)
        if volume_outline_issues:
            raise LedgerError("volume_outline_contract_violation", "plan extension violates the signed full-book volume outline", {"errors": volume_outline_issues})
        scale_errors = scale_contract_errors(candidate, self.load_config())
        if scale_errors:
            raise LedgerError(
                "book_scale_contract_violation",
                "plan extension violates the signed book scale",
                {"errors": scale_errors},
            )
        story_errors = plan_story_map_errors(candidate, required=contract_required)
        if story_errors:
            raise LedgerError(
                "event_spine_contract_violation",
                "plan extension breaks the signed four-layer event spine",
                {
                    "errors": story_errors,
                    "hint": (
                        "each error pinpoints the failing field (message + phase_index/missing_field); "
                        "a phase brief needs ALL of: id/name/objective/climax/story_stage/tension_stage/"
                        "chapter_start/chapter_end/entry_state/exit_state/tension_change/event_changes "
                        "+ mainline_event_ref/subplot_event_refs/timeline_event_refs"
                    ),
                },
            )
        plan["chapters"] = chapters
        if "expansion_batches" in candidate:
            plan["expansion_batches"] = candidate["expansion_batches"]
        if "phases" in candidate:
            plan["phases"] = candidate["phases"]
        if volumes:
            plan["volumes"] = candidate["volumes"]
        if character_profiles:
            plan["character_profiles"] = candidate["character_profiles"]
        self.save_plan(plan)
        return {
            "added": added,
            "max_chapter": max(added) if added else max(
                (int(i.get("chapter") or 0) for i in existing if int(i.get("chapter") or 0) > 0), default=0),
            "phase_id": str((phase_brief or {}).get("id") or "") or None,
            "volumes_updated": sorted(str(k) for k in (volumes or {})),
            "character_profiles_updated": sorted(character_profiles or {}),
            "warnings": warnings,
        }

    def chapter_plan(self, chapter: int) -> dict[str, Any]:
        plan = self.load_plan()
        for item in plan["chapters"]:
            if int(item.get("chapter") or 0) == chapter:
                return item
        raise LedgerError(
            "missing_chapter_plan",
            f"plan has no chapter {chapter}; plan JSON is the beat source of truth",
        )

    def load_kb(self) -> list[dict[str, Any]]:
        if not self.kb_path.exists():
            return []
        data = read_json(self.kb_path)
        if isinstance(data, list):
            return validate_kb_cards(data)
        if isinstance(data, dict) and isinstance(data.get("cards"), list):
            return validate_kb_cards(data["cards"])
        raise LedgerError("invalid_kb", "kb must be {cards:[...]} or a card list")

    def save_kb(self, cards: list[dict[str, Any]], *, source_fingerprint: str | None = None) -> None:
        """写 cards.json。

        `source_fingerprint` 记录"这份 cards 由哪一版 canon 源编译而来"（见
        `pipeline._canon_source_drift`）。不传时**沿用文件里已有的值**——否则
        `init` 这类只做搬运的调用方会顺手抹掉凭证，让同步守卫永远报 available=False。
        """
        payload: dict[str, Any] = {"cards": validate_kb_cards(cards)}
        recorded = source_fingerprint
        if recorded is None and self.kb_path.exists():
            try:
                existing = read_json(self.kb_path)
            except LedgerError:
                existing = None
            if isinstance(existing, dict):
                recorded = str(existing.get("source_fingerprint") or "") or None
        if recorded:
            payload["source_fingerprint"] = recorded
        atomic_json(self.kb_path, payload)

    def canon_fingerprint(self) -> str:
        """正典指纹：全部设定卡的 id+正文哈希。

        commit 时把当时的指纹记进事件，`book audit` 拿它与当前正典比对——正典在任何一章
        commit 之后被改（改价、改等级、新增硬禁），就会报 canon_drift，提示已写正文/账本
        可能仍按旧正典，需要一次一致性复核。只作提示，不推断哪几章受影响。
        """
        from ..infra.util import sha256_text

        parts = sorted(f"{c.get('id')}|{c.get('body')}" for c in self.load_kb() if isinstance(c, dict))
        return "sha256:" + sha256_text("\n".join(parts))

    def load_voice_profile(self) -> dict[str, Any]:
        """读 `book/memory/voice.json`。缺文件返回空 dict（pack 随后以 `missing_voice` 拒绝开章）。

        不做旧格式迁移：必须是对象。缺 `session_notes` 视为尚无会话笔记（init 刚写完就是这样）。
        """
        if not self.voice_memory_path.exists():
            return {}
        data = read_json(self.voice_memory_path)
        if not isinstance(data, dict):
            raise LedgerError("invalid_voice", "voice.json must be an object")
        return data

    def _voice_strings(self, key: str) -> list[str]:
        items = self.load_voice_profile().get(key)
        if not isinstance(items, list):
            return []
        return [str(x).strip() for x in items if str(x).strip()]

    def load_voice_concepts(self) -> list[str]:
        """长驻概念：只由 `init` 与 `voice apply` 写，不参与滑窗淘汰。"""
        return self._voice_strings("concepts")

    def load_voice_session_notes(self) -> list[str]:
        """会话笔记由 ack / 写者回灌写入；完整持久化，不按旧cap驱逐。"""
        return self._voice_strings("session_notes")

    def save_voice_session_notes(self, notes: list[str]) -> None:
        profile = self.load_voice_profile()
        profile["session_notes"] = list(notes)
        self.save_voice_profile(profile)

    def save_voice_profile(self, profile: dict[str, Any]) -> None:
        atomic_json(self.voice_memory_path, profile)

    def chapter_md_path(self, chapter: int) -> Path:
        return self.chapters_dir / self._volume_folder(chapter) / f"ch-{chapter:04d}.md"

    def chapter_meta_path(self, chapter: int) -> Path:
        return self.chapters_dir / self._volume_folder(chapter) / f"ch-{chapter:04d}.meta.json"

    def meta_path(self, chapter: int) -> Path:
        return self.chapter_meta_path(chapter)

    def ack_path(self, chapter: int) -> Path:
        return self.acks_dir / self._volume_folder(chapter) / f"ch-{chapter:04d}.json"

    def pack_archive_path(self, chapter: int) -> Path:
        return self.pack_dir / f"ch-{chapter:04d}.json"

    def draft_pack_path(self, chapter: int) -> Path:
        """内容写作阶段的只读视图（零 voice_* 字段），与 canonical pack 同 hash。"""
        return self.pack_dir / f"draft-{chapter:04d}.json"

    def polish_pack_path(self, chapter: int) -> Path:
        """润色阶段的只读视图（含 voice_writing_text 文风手册全文与输出契约）。"""
        return self.pack_dir / f"polish-{chapter:04d}.json"

    def assemble_pack_path(self, chapter: int) -> Path:
        """组装阶段的只读视图（零文风字段，含 pack_hash 与输出契约）。"""
        return self.pack_dir / f"assemble-{chapter:04d}.json"

    def assemble_brief_path(self, chapter: int) -> Path:
        """组装简报（assemble 视图的线性文本渲染）：worker 一次读全的读取形态。"""
        return self.pack_dir / f"assemble-{chapter:04d}.brief.txt"

    def draft_text_path(self, chapter: int) -> Path:
        """阶段一草稿正文（纯文本），只被阶段二读取。"""
        from .artifact_path import ArtifactPath
        return ArtifactPath(self.staging_dir / f"draft-{chapter:04d}.txt")

    def polished_text_path(self, chapter: int) -> Path:
        """阶段二润色后的终稿正文（纯文本），供 ack/人工核对阶段隔离。"""
        from .artifact_path import ArtifactPath
        return ArtifactPath(self.staging_dir / f"polished-{chapter:04d}.txt")

    def stage_revision_path(self, chapter: int, rev: int) -> Path:
        """返工轮转档：plot_fix/rewrite/换包重排时保留的旧稿（真源托管）。

        返工 worker 的定点修复底稿——复制回 draft_output_path 后只修引文所指的
        BLOCKER、其余逐字保留；机检闸门照常全跑，质量不因保留旧稿而降低。
        """
        from .artifact_path import ArtifactPath
        return ArtifactPath(self.staging_dir / f"draft-{chapter:04d}.rev{rev}.txt")

    def latest_stage_revision(self, chapter: int) -> Path | None:
        """本章最新的轮转档（rev 号最大者）；从未轮转过则 None。"""
        prefix = f"staging/draft-{chapter:04d}.rev"
        best = -1
        for key in self.database.paths():
            if not key.startswith(prefix) or not key.endswith(".txt"):
                continue
            num = key[len(prefix):-len(".txt")]
            if num.isdigit():
                best = max(best, int(num))
        if best < 0:
            return None
        return self.stage_revision_path(chapter, best)

    def accept_stage_text(self, path: Path, text: str) -> None:
        key = Path(str(path)).relative_to(Path(str(self.book))).as_posix()
        self.database.write(key, text.encode("utf-8"))
        self.database.project(key, text.encode("utf-8"))

    def submit_check_receipt_path(self, chapter: int) -> Path:
        """旧版 check-submit 回执路径；仅用于清理遗留 staging 文件。"""
        return self.staging_dir / f"submit-check-{int(chapter):04d}.json"

    def style_metrics_path(self, chapter: int) -> Path:
        """机检 ✗ 清单（staging 文件，polish-submit 硬红线失败时写、交给文风编辑定向返工）。"""
        return self.staging_dir / f"style-metrics-{chapter:04d}.json"

    def polish_anchor_path(self, chapter: int) -> Path:
        """润色收口的内容锚点问题清单（staging 文件，交给文风编辑定向返工）。"""
        return self.staging_dir / f"polish-anchors-{chapter:04d}.json"

    def summary_path(self, chapter: int) -> Path:
        return self.summaries_dir / self._volume_folder(chapter) / f"ch-{chapter:04d}.json"

    def volume_meta(self, chapter: int) -> dict[str, Any]:
        """本章所在卷的顶层可选元数据（plan.volumes）。

        卷号归一解析：章拍 volume（缺省 1）与 plan.volumes 键都先折成卷号再匹配，
        因此 hatch 的旧键 "vol-01" 与目录名 "vol-0001"、整数 1、"第一卷" 互相等价。
        没有匹配项返回空 dict，调用方回退全局 volume_spine。章节级 goal/recap 始终优先于这里。
        """
        volumes = (self.load_plan().get("volumes") or {})
        if not isinstance(volumes, dict):
            return {}
        try:
            raw = self.chapter_plan(chapter).get("volume", 1)
        except LedgerError:
            raw = 1
        item = volume_entry_for(volumes, raw)
        if item is None:
            return {}
        meta: dict[str, Any] = {}
        for field in ("spine", "goal", "recap"):
            value = str(item.get(field) or "").strip()
            if value:
                meta[field] = value
        return meta

    def _volume_folder(self, chapter: int) -> str:
        """每章所属卷的文件夹名（如 vol-0001）。章未标 volume 时按第一卷处理。"""
        raw = None
        try:
            raw = self.chapter_plan(chapter).get("volume", 1)
        except LedgerError:
            raw = 1
        return _volume_label(raw)



def empty_head() -> dict[str, Any]:
    return {
        "schema": "novel-ledger.head.v1",
        "status": "active",
        "phase": PHASE_IDLE,
        "chapter": 0,
        "last_committed_ch": 0,
        "last_acked_ch": 0,
        "pack_hash": None,
        "prose_hash": None,
        "rewrite_count": 0,
        "reopen_count": 0,
        "style_metrics_retry": 0,
        "style_metrics_pending": False,
        "blocked": None,
        "updated_at": now_ts(),
    }
