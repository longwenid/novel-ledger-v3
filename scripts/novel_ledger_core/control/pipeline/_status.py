# 从 pipeline.py 按域拆出（行为不变；全量测试为等价性闸门）。域：status
from __future__ import annotations

from pathlib import Path
from typing import Any
from ... import (__version__)
from ...content.reviews import (review_summary)
from ...infra.store import (BookStore)
from ...infra.util import (ok, sha256_text)

from ._common import (
    _book_words_written,
    _volume_watermark,
)
from ._usage import (
    _USAGE_DETAIL_WINDOW,
    _usage_summary,
)

def _runtime_identity() -> dict[str, Any]:
    """Expose a portable control-plane identity for installation verification.

    The path catches a stale environment override/symlink; the digest distinguishes two source
    trees that accidentally share a human version.  It deliberately fingerprints the executable
    control plane and its protocol entry points, not mutable project data.
    """
    root = Path(__file__).resolve().parents[4]  # pipeline包比单文件深一级
    members = (
        "SKILL.md",
        "references/execution-architecture.md",
        "scripts/novel_ledger.py",
        "scripts/novel_ledger_core/__init__.py",
        "scripts/novel_ledger_core/control/cli.py",
        "scripts/novel_ledger_core/control/pipeline/__init__.py",
        "scripts/novel_ledger_core/infra/store.py",
        "scripts/novel_ledger_core/infra/database.py",
        "scripts/novel_ledger_core/content/recall.py",
    )
    parts: list[str] = []
    present: list[str] = []
    for rel in members:
        path = root / rel
        if path.is_file():
            present.append(rel)
            parts.append(f"{rel}\0{sha256_text(path.read_text(encoding='utf-8'))}")
        else:
            parts.append(f"{rel}\0missing")
    return {
        "skill": "novel-ledger-v3",
        "version": __version__,
        "skill_root": str(root),
        "control_fingerprint": "sha256:" + sha256_text("\n".join(parts)),
        "fingerprinted_files": present,
    }


def status(store: BookStore, *, card: bool = False) -> dict[str, Any]:
    head = store.read_head()
    phase = head.get("phase")
    last_c = int(head.get("last_committed_ch") or 0)
    try:
        planned = sorted(
            {
                int(item.get("chapter") or 0)
                for item in (store.load_plan().get("chapters") or [])
                if int(item.get("chapter") or 0) > 0
            }
        )
    except (KeyError, TypeError, ValueError):
        planned = []
    # 拆成计数与列表两个字段：`plan_remaining_count` 才是能跟 `plan_low_water` 比大小的那个。
    # 早前只有一个 `plan_remaining`（列表）与 `plan_low_water`（整数）并列，编排者拿两者
    # 直接比较——Python 抛 TypeError，JavaScript 静默转 true，无人值守时一直误判水位告急。
    remaining = [c for c in planned if c > last_c]
    # 全书目标字数与已写进度：只读各章 meta 的 word_count（不重读正文）。book_words
    # 既是规划参照，也是计划耗尽/全书完结的规模门禁。
    cfg = store.load_config()
    book_words = int(cfg.get("book_words") or 0)
    book_words_written = _book_words_written(store)
    payload = ok(
        phase=phase,
        status=head.get("status"),
        chapter=head.get("chapter"),
        last_committed_ch=head.get("last_committed_ch"),
        last_acked_ch=head.get("last_acked_ch"),
        pack_hash=head.get("pack_hash"),
        prose_hash=head.get("prose_hash"),
        rewrite_count=head.get("rewrite_count") or 0,
        reopen_count=head.get("reopen_count") or 0,
        style_metrics_retry=head.get("style_metrics_retry") or 0,
        style_metrics_pending=bool(head.get("style_metrics_pending")),
        polish_anchor_retry=head.get("polish_anchor_retry") or 0,
        polish_anchor_pending=bool(head.get("polish_anchor_pending")),
        blocked=head.get("blocked"),
        require_ack=bool(cfg.get("require_ack", True)),
        plan_max_chapter=max(planned) if planned else 0,
        plan_remaining_count=len(remaining),
        plan_remaining_chapters=remaining,
        plan_low_water=cfg.get("plan_low_water"),
        schema_version=int(cfg.get("schema_version") or 0),
        book_words=book_words,
        book_words_written=book_words_written,
        book_words_remaining=max(book_words - book_words_written, 0),
        book_words_progress=(
            round(book_words_written / book_words, 4) if book_words > 0 else 0.0
        ),
        storage={"engine": "sqlite", "database_path": str(store.database_path), "authority": "database", "file_role": "exports_and_inputs"},
        # 配置影子漂移：磁盘 config.json 被手工改过而数据库真源未变——散改不生效，
        # 也不能让它无声（真实项目曾把这种漂移误诊为缓存/读抖动，整夜排查）。
        config_shadow=store.config_shadow_divergence(),
        # 托管文档投影漂移（双向）：越层直写与散改投影都在开跑/巡检时点名。
        document_shadow=store.document_shadow_divergence(),
        runtime=_runtime_identity(),
        execution_mode=cfg.get("execution_mode"),
        editorial_review=review_summary(store),
        # 卷合同水位：当前卷剩余章数 + 下一卷卷脊是否已签（未启用卷合同时为 None）。
        volume_watermark=_volume_watermark(store, head),
        # token 账本汇总；逐请求预算由 `usage-record` 与下一模型动作前的 guard 执行。
        usage=_usage_summary(store, detail_window=_USAGE_DETAIL_WINDOW),
    )
    if card:
        return _status_card(payload)
    return payload


# 只在诊断时才需要。卡字段与宿主核对口径一一对应：相位/章号/停线/进度水位。
_STATUS_CARD_KEYS = (
    "ok", "phase", "status", "chapter", "blocked", "last_committed_ch", "last_acked_ch",
    "rewrite_count", "plan_remaining_count", "plan_low_water", "plan_max_chapter",
    "book_words_written", "book_words", "book_words_progress",
)


def _status_card(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in _STATUS_CARD_KEYS if key in payload}


__all__ = [
    '_runtime_identity',
    'status',
    '_STATUS_CARD_KEYS',
    '_status_card',
]
