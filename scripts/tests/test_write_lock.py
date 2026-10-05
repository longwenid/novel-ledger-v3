"""写锁：并发两个写命令必须一个成功、一个 `locked`，不能互相覆盖。

历史缺陷（质检 F-04）：`fcntl` 是 POSIX 专有模块，代码在导入失败时把锁降级成
"直接放行"，而 `references/recovery.md` 无平台限定地承诺 `locked` 语义。
在 Windows 上并发跑 `chapter next` 与 `book resync-baseline` 会交错写
HEAD/events/snapshot——那是最硬的一条不变量（事件溯源），却只在 POSIX 上有守卫。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.infra import store as store_mod
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError


def _store(tmp_path: Path) -> BookStore:
    return BookStore(tmp_path / "novel")


def test_flock_path_rejects_concurrent_writer(tmp_path: Path):
    store = _store(tmp_path)
    with store.exclusive_lock() as lock_path:
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
        with pytest.raises(LedgerError) as exc:
            with store.exclusive_lock():
                pass
        assert exc.value.code == "locked"
        assert exc.value.details["holder"]

    # 释放后必须能重新拿到（锁不是一次性的）
    with store.exclusive_lock():
        pass


def test_directory_lock_fallback_rejects_concurrent_writer(tmp_path: Path, monkeypatch):
    """没有 fcntl 的平台必须退化为真互斥，而不是静默放行。"""
    monkeypatch.setattr(store_mod, "fcntl", None)
    # 本机是 Windows 时 msvcrt 可用，只关掉 fcntl 还走不到目录锁分支；
    # 目录锁是"两个原语都缺失"的兜底，测试必须把两个原语一起关掉。
    monkeypatch.setattr(store_mod, "msvcrt", None)
    store = _store(tmp_path)
    lock_dir = store._directory_lock_path()

    with store.exclusive_lock():
        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        with pytest.raises(LedgerError) as exc:
            with store.exclusive_lock():
                pass
        assert exc.value.code == "locked"
        assert str(lock_dir) in exc.value.details["hint"]

    assert not lock_dir.exists(), "释放后目录锁必须清干净"
    with store.exclusive_lock():
        pass


@pytest.mark.skipif(os.name != "posix", reason="非 POSIX 平台不做进程探活，保守不回收")
def test_directory_lock_reclaims_dead_owner(tmp_path: Path, monkeypatch):
    """断电/崩溃留下的残锁：确认持有者已死才回收，避免人工解锁卡住长跑。"""
    monkeypatch.setattr(store_mod, "fcntl", None)
    monkeypatch.setattr(store_mod, "msvcrt", None)
    store = _store(tmp_path)
    lock_dir = store._directory_lock_path()
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": 999_999, "ts": 0}),
        encoding="utf-8",
    )

    with store.exclusive_lock():
        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()

    assert not lock_dir.exists()


def test_directory_lock_reclaims_owner_reported_dead(tmp_path: Path, monkeypatch):
    """残锁回收的**判定分支**必须跨平台可测：`_pid_alive` 一旦恒返回 True 就永不回收。

    上面那条真进程探活的用例只在 POSIX 跑（Windows 不做探活），所以在 Windows 上
    它整条被 skip，`_pid_alive` 被改成恒 True 也无人发现。这里不依赖平台信号：
    直接把 `_pid_alive` 打桩成"持有者已死"，验证回收分支本身没有被关掉。
    """
    monkeypatch.setattr(store_mod, "fcntl", None)
    monkeypatch.setattr(store_mod, "msvcrt", None)
    store = _store(tmp_path)
    lock_dir = store._directory_lock_path()
    lock_dir.mkdir(parents=True)
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": 999_999, "ts": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(store_mod, "_pid_alive", lambda pid: False)

    with store.exclusive_lock():
        owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()

    assert not lock_dir.exists()
