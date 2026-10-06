"""Zero-dependency test runner: discovers test_*.py under scripts/tests and runs
every `test_*` function. Supports the fixture subset used by this suite:
tmp_path, capsys, monkeypatch, and module-level @pytest.fixture functions.

Usage: python run_all_tests.py [substring ...]   (substring filters file names)
"""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # novel_ledger_core importable
sys.path.insert(0, str(HERE.parent.parent))  # `scripts.tests...` package imports


def _install_pytest_stub() -> None:
    """Provide a minimal `pytest` for tests that import it directly."""
    import types

    if "pytest" in sys.modules:
        return
    try:
        import pytest_compat as compat  # type: ignore
    except Exception:  # noqa: BLE001
        compat = None

    stub = types.ModuleType("pytest")
    if compat is not None:
        stub.fixture = compat.fixture
        stub.mark = compat.mark
        stub.raises = compat.raises
    else:  # pragma: no cover
        stub.fixture = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
        stub.mark = types.SimpleNamespace(
            __getattr__=lambda name: lambda *a, **k: (lambda fn: fn)
        )
    stub.approx = lambda value, rel=None, abs=None: value  # noqa: A002
    stub.skip = lambda reason="": (_ for _ in ()).throw(AssertionError(f"skipped: {reason}"))

    class _Param:
        def __init__(self, *values, **kwargs):
            self.values = values
            self.kwargs = kwargs

        def __iter__(self):
            return iter(self.values)

    stub.param = lambda *values, **kwargs: _Param(*values, **kwargs)
    sys.modules["pytest"] = stub


_install_pytest_stub()


class _Capsys:
    def __init__(self):
        self._stdout = io.StringIO()
        self._stderr = io.StringIO()

    def readouterr(self):
        class _R:
            def __init__(self, out, err):
                self.out = out
                self.err = err

        return _R(self._stdout.getvalue(), self._stderr.getvalue())


class _Monkeypatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        if not isinstance(target, type) and not hasattr(target, "__dict__") and not isinstance(target, dict):
            raise TypeError("unsupported setattr target")
        if isinstance(target, dict):
            old = target.get(name, KeyError)
            self._undo.append(lambda: (target.__setitem__(name, old) if old is not KeyError else target.pop(name, None)))
            target[name] = value
        else:
            old = getattr(target, name)
            self._undo.append(lambda: setattr(target, name, old))
            setattr(target, name, value)

    def chdir(self, path):
        old = Path.cwd()
        self._undo.append(lambda: __import__("os").chdir(old))
        __import__("os").chdir(path)

    def undo(self):
        for fn in reversed(self._undo):
            fn()
        self._undo.clear()


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def _resolve_fixtures(fn, mod, stack):
    """Build kwargs: tmp_path / capsys / monkeypatch / module-level fixture fns."""
    kwargs = {}
    tmp_stack, cap_stack, mp_stack = [], [], []
    for pname in inspect.signature(fn).parameters:
        if pname == "tmp_path":
            d = Path(tempfile.mkdtemp(prefix="nlv3-test-"))
            tmp_stack.append(d)
            kwargs[pname] = d
        elif pname == "capsys":
            cap = _Capsys()
            cap_stack.append(cap)
            kwargs[pname] = cap
        elif pname == "monkeypatch":
            mp = _Monkeypatch()
            mp_stack.append(mp)
            kwargs[pname] = mp
        else:
            provider = getattr(mod, pname, None)
            if callable(provider) and getattr(provider, "__module__", "") == mod.__name__:
                stack.append(provider)
                nested, *_ = _resolve_fixtures(provider, mod, stack)
                kwargs[pname] = provider(**nested)
    return kwargs, tmp_stack, cap_stack, mp_stack


def call_test(fn, mod):
    kwargs, tmps, caps, mps = _resolve_fixtures(fn, mod, [])
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            fn(**kwargs)
        finally:
            for mp in mps:
                mp.undo()


def main() -> int:
    filters = [a for a in sys.argv[1:] if not a.startswith("-")]
    files = sorted(p for p in HERE.glob("test_*.py") if not p.name.startswith("_"))
    if filters:
        files = [p for p in files if any(f in p.name for f in filters)]
    passed = failed = 0
    failures: list[tuple[str, str]] = []
    for path in files:
        mod = load_module(path)
        for name, fn in vars(mod).items():
            if not name.startswith("test_") or not callable(fn):
                continue
            t0 = time.time()
            try:
                call_test(fn, mod)
                passed += 1
                print(f"ok   {path.name}::{name} ({time.time()-t0:.1f}s)")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                tb = traceback.format_exc()
                failures.append((f"{path.name}::{name}", f"{type(exc).__name__}: {exc}"))
                print(f"FAIL {path.name}::{name} ({time.time()-t0:.1f}s)\n{tb}")
    print(f"\n{passed} passed, {failed} failed")
    for name, err in failures:
        print(f"  FAILED {name}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
