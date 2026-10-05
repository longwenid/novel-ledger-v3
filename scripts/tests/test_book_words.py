from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.pipeline import status
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import DEFAULT_CONFIG, BookStore
from novel_ledger_core.infra.util import LedgerError
from tests.decoupled_helpers import make_chapter, make_plan, write_plan


def _plan() -> dict:
    return make_plan([make_chapter(1, tags=("市集", "凭据"))])


def _write_plan(tmp_path: Path) -> Path:
    return write_plan(tmp_path, _plan())


def _init(tmp_path: Path, name: str, **kwargs) -> BookStore:
    proj = tmp_path / name
    proj.mkdir()
    init_project(proj, plan_path=_write_plan(tmp_path), protagonist="主角", **kwargs)
    return BookStore(proj)


def test_default_book_words_is_two_million():
    """新书未指定时，全书目标字数默认 200 万。"""
    assert DEFAULT_CONFIG["book_words"] == 2_000_000


def test_init_writes_default_book_words_to_config(tmp_path: Path):
    store = _init(tmp_path, "default")
    assert store.load_config()["book_words"] == 2_000_000


def test_init_book_words_override_persists(tmp_path: Path):
    store = _init(tmp_path, "custom", book_words=500_000)
    assert store.load_config()["book_words"] == 500_000


def test_init_rejects_non_positive_book_words(tmp_path: Path):
    """非正数字数在开书时即拒，且不留半初始化项目。"""
    proj = tmp_path / "badwords"
    proj.mkdir()
    with pytest.raises(LedgerError) as exc:
        init_project(
            proj,
            plan_path=_write_plan(tmp_path),
            protagonist="主角",
            book_words=0,
        )
    assert exc.value.code == "invalid_book_words"
    assert not BookStore(proj).head_path.exists()


def test_status_reports_book_words_progress(tmp_path: Path):
    """status 暴露目标字数、已写字数、剩余与进度；已写量累加各章 meta 的 word_count。"""
    store = _init(tmp_path, "progress", book_words=1000)
    # 直接落一章 meta，模拟已提交正文的 word_count，避免跑完整写作链。
    store.chapter_meta_path(1).parent.mkdir(parents=True, exist_ok=True)
    store.chapter_meta_path(1).write_text(
        json.dumps({"chapter": 1, "word_count": 250}, ensure_ascii=False),
        encoding="utf-8",
    )
    st = status(store)
    assert st["book_words"] == 1000
    assert st["book_words_written"] == 250
    assert st["book_words_remaining"] == 750
    assert st["book_words_progress"] == 0.25


def test_status_book_words_zero_when_unset(tmp_path: Path):
    """未设目标（book_words=0）时进度为 0，不出现除零。"""
    store = _init(tmp_path, "zeroset")
    cfg = store.load_config()
    cfg["book_words"] = 0
    store.save_config(cfg)
    st = status(store)
    assert st["book_words"] == 0
    assert st["book_words_progress"] == 0.0
    assert st["book_words_remaining"] == 0
