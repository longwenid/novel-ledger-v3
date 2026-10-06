from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

# 仓库根的 pytest.ini 已把 scripts 加进 pythonpath；这里再兜一次，
# 让「不经由根 pytest.ini」的调用方式（直接 pytest 某个测试文件、或从 scripts/ 下跑）也能 import。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _disable_plan_low_water_in_tests():
    from novel_ledger_core.infra import store as store_mod

    # 测试专用数据库加速：默认 fsync 在千余个用例的建库+章节循环上是套件时长
    # 大头（实测单用例地板 0.66s）。测试不需要崩溃耐久——真源在事件链，git 兜底；
    # 生产环境不设此变量，durability 语义不变。
    os.environ.setdefault("NOVEL_LEDGER_TEST_FAST_DB", "1")

    old = store_mod.DEFAULT_CONFIG.get("plan_low_water")
    store_mod.DEFAULT_CONFIG["plan_low_water"] = 0
    old_style = store_mod.DEFAULT_CONFIG.get("style_check")
    store_mod.DEFAULT_CONFIG["style_check"] = "off"
    # 仓库默认 execution_mode=stage-agent；既有管线用例测的是兼容阶段语义，
    # 统一固定 inline/v1；真正隔离/v2 的调用、恢复与能力边界由
    # test_editorial_architecture 的真实 CLI 子进程集成测试覆盖。
    old_mode = store_mod.DEFAULT_CONFIG.get("execution_mode")
    store_mod.DEFAULT_CONFIG["execution_mode"] = "inline"
    # Existing fixtures exercise the v1 five-field submit contract. The stage
    # isolation suite explicitly selects v2 and tests its required review field.
    old_review = store_mod.DEFAULT_CONFIG.get("review_contract_version")
    store_mod.DEFAULT_CONFIG["review_contract_version"] = 1
    yield
    store_mod.DEFAULT_CONFIG["plan_low_water"] = old
    store_mod.DEFAULT_CONFIG["style_check"] = old_style
    store_mod.DEFAULT_CONFIG["execution_mode"] = old_mode
    store_mod.DEFAULT_CONFIG["review_contract_version"] = old_review


# --- 临时目录兼容层：让维护套件在受管沙箱 / Windows 上真的跑得起来 -----
#
# 症状（现场实测）：`make test` 整批报废——一次全量运行 678 次
#   PermissionError: [WinError 5] ...\pytest-of-<user>   (_pytest/pathlib.py:175 extract_suffixes)
# 全部发生在 `tmp_path` / `tmp_path_factory` 的 setup 阶段。
#
# 根因不是路径也不是权限策略，而是 **mode**：pytest 建临时目录一律用 0o700
# （`_pytest/tmpdir.py:getbasetemp` 的 `mkdir(mode=0o700)`、`TempPathFactory.mktemp`
# 的 `mkdir(mode=0o700)`、`_pytest/pathlib.py:make_numbered_dir(root, prefix, mode=0o700)`），
# 而在这类宿主沙箱里，0o700 建出来的目录**既不可写入也不可枚举**（os.listdir →
# WinError 5），**也无法再 chmod 修回**（os.chmod 同样 WinError 5），连删除都要提权；
# 默认 mode 建的目录一切正常（三组对照实测：默认 / 0o755 / 0o777 全通过）。
#
# 后果比"少跑几个用例"严重：维护者拿不到任何正式质量门禁，只能临时写脚本人肉验算，
# pytest 的绿灯在提交与决策日志里失去可信度（本项目历史决策日志两次拒绝采信
# "495 passed / 496 passed"，正是这个后果的产物）。
#
# 修法：用自带的临时目录工厂替换 tmp_path_factory，只用默认 mode 建目录。
# 真实访问边界由宿主 ACL / 沙箱决定，去掉 0o700 不改变隔离性；每条用例仍拿到独占目录。
# 根目录约定与 scripts/tests/host_tmp.py 共用，可用 NOVEL_LEDGER_TEST_TMP 覆盖。


class _DefaultModeTmpFactory:
    """tmp_path_factory 的最小替身：只用默认 mode 建目录。

    pytest 自带的 `tmp_path` fixture 在 teardown 里会读 `_retention_policy` /
    `_retention_count`，所以这两个属性必须存在；这里取 count=0（每条用例结束即删），
    避免跨会话残留的旧目录被同名用例复用而污染结果。
    """

    _retention_policy = "all"
    _retention_count = 0

    def __init__(self, root: Path) -> None:
        self._root = root
        self._taken: set[str] = set()

    def getbasetemp(self) -> Path:
        return self._root

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        import re

        name = re.sub(r"\W", "_", str(basename)).strip("_")[:30] or "tmp"
        candidate = name
        index = 0
        if numbered:
            while candidate in self._taken:
                index += 1
                candidate = f"{name}{index}"
        self._taken.add(candidate)
        path = self._root / candidate
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _trace(self, *args, **kwargs) -> None:  # pytest 内部调试钩子，替身里不需要
        return None


@pytest.fixture(scope="session")
def tmp_path_factory() -> "_DefaultModeTmpFactory":
    # 与 scripts/tests/host_tmp.py 共用同一个根目录约定（NOVEL_LEDGER_TEST_TMP）。
    from tests.host_tmp import temp_root

    # 每次会话用一个独占子目录并在结束时删掉：既不残留旧用例目录（上一轮崩溃留下的
    # 同名目录会让本轮 `project.mkdir()` 直接 FileExistsError），也不无限堆积。
    run_root = temp_root() / f"run-{os.getpid()}"
    shutil.rmtree(run_root, ignore_errors=True)
    run_root.mkdir(parents=True, exist_ok=True)
    yield _DefaultModeTmpFactory(run_root)
    shutil.rmtree(run_root, ignore_errors=True)


def bash_usable() -> bool:
    """本机是否有一个真能跑起来的 bash（Git Bash 在部分受限沙箱里起不来）。

    现场实测：`bash.exe: *** fatal error - CreateFileMapping S-1-5-21-...: Win32 error 5`。
    依赖 bash 的用例应当在启动前先问这一句，把"环境不支持"报成带原因的 skip，
    而不是报成一条看起来像回归的失败。
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["bash", "-c", "printf nl-bash-ok"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "nl-bash-ok" in (proc.stdout or "")


requires_bash = pytest.mark.skipif(
    not bash_usable(),
    reason=(
        "本机没有可用的 bash（受限沙箱下 Git Bash 起不来：CreateFileMapping WinError 5）；"
        "该用例需要真实 shell 才能覆盖，跳过而不是伪装成通过"
    ),
)


def patch_pipeline_name(monkeypatch, name, value):
    """在拆包后的 pipeline 包内改绑一个名字（等价于单文件时代 setattr(pipeline, name)）。

    单文件时代所有函数共享一个模块命名空间；拆包后同一名字会出现在定义子模块与
    若干导入子模块里，只 patch 包属性不影响任何调用方。这里遍历全部子模块，
    凡 __dict__ 里持有该名字的一并改绑，调用点保持原样。
    """
    import novel_ledger_core.control.pipeline as pipeline_pkg

    for attr in vars(pipeline_pkg).values():
        mod = getattr(attr, "__dict__", None)
        if isinstance(mod, dict) and getattr(attr, "__name__", "").startswith(
            "novel_ledger_core.control.pipeline."
        ) and name in mod:
            monkeypatch.setattr(attr, name, value)
    monkeypatch.setattr(pipeline_pkg, name, value)
