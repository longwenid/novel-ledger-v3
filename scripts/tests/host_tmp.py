"""宿主安全临时目录：给"自己造临时目录"的用例与维护脚本用。

为什么需要它（现场实测：Windows + Python 3.12 + 宿主文件沙箱）：
`tempfile.TemporaryDirectory()` / `tempfile.mkdtemp()` **固定按 0o700 建目录**，而在这类
受管沙箱里，0o700 建出来的目录**不可写入、不可枚举、不可 chmod 修回、也不可删除**
（分别为 `[Errno 13]`、`[WinError 5]`、`[WinError 5]`、`[WinError 5]`）；默认 mode
建出来的目录一切正常。于是：

- pytest 的 `tmp_path` / `tmp_path_factory`（同样写死 0o700）全数 setup error —— 一次
  全量运行 678 次 `PermissionError`，`make test` 直接报废；
- 直接用 `tempfile` 造沙箱的维护脚本（`.dev/pressure/run.py`）同样在第一步就炸。

被删不掉的垃圾目录还会永久留在工作区里，连清理都要提权。

修法只有一条：**建临时目录时不要传 mode**。本模块与 `conftest.py` 的临时目录兼容层
共用同一个根目录约定（`NOVEL_LEDGER_TEST_TMP`）。
"""

from __future__ import annotations

import itertools
import os
import tempfile
from pathlib import Path

ENV_ROOT = "NOVEL_LEDGER_TEST_TMP"
_DEFAULT_ROOT_NAME = "novel-ledger-tmp"
_counter = itertools.count()


def temp_root() -> Path:
    """维护期临时文件的共享根目录；可用 NOVEL_LEDGER_TEST_TMP 覆盖。"""
    root = Path(os.environ.get(ENV_ROOT) or (Path(tempfile.gettempdir()) / _DEFAULT_ROOT_NAME))
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_mode_tempdir(prefix: str = "tmp-", parent: Path | None = None) -> Path:
    """建一个**默认 mode**的独占临时目录（默认 mode = 这类沙箱里唯一可用的一种）。"""
    base = Path(parent) if parent is not None else temp_root()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{prefix}{os.getpid()}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    return path
