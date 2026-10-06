"""Database-backed artifact addresses with explicit filesystem projections.

The Path interface is retained for existing domain operations. It does not
patch pathlib globally. Staging input, process locks and driver output stay
ordinary files; accepted output and all durable project artifacts use SQLite.
"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from .database import BookDatabase


class _DocumentBuffer:
    def __init__(self, path, mode, encoding, errors, newline):
        self.path, self.mode = path, mode
        self.binary = "b" in mode
        self.encoding = encoding or "utf-8"
        self.errors = errors or "strict"
        exists = path.exists()
        if "x" in mode and exists:
            raise FileExistsError(str(path))
        if "r" in mode and not exists:
            raise FileNotFoundError(str(path))
        data = path.read_bytes() if exists and "w" not in mode and "x" not in mode else b""
        self.buffer = io.BytesIO(data) if self.binary else io.StringIO(data.decode(self.encoding, self.errors), newline=newline)
        self.writable = any(c in mode for c in "wax+")
        if "a" in mode:
            self.buffer.seek(0, io.SEEK_END)
        self.dirty = self.writable and ("w" in mode or "x" in mode or not exists)

    def write(self, value):
        if not self.writable:
            raise io.UnsupportedOperation("not writable")
        if "a" in self.mode:
            self.buffer.seek(0, io.SEEK_END)
        self.dirty = True
        return self.buffer.write(value)

    def truncate(self, size=None):
        if not self.writable:
            raise io.UnsupportedOperation("not writable")
        self.dirty = True
        return self.buffer.truncate(size)

    def flush(self):
        if self.dirty:
            data = self.buffer.getvalue()
            self.path.write_bytes(data if self.binary else data.encode(self.encoding, self.errors))
            self.dirty = False

    def close(self):
        if not self.buffer.closed:
            self.flush()
            self.buffer.close()

    def __getattr__(self, name):
        return getattr(self.buffer, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close()

    def __iter__(self):
        return iter(self.buffer)


class ArtifactPath(type(Path())):
    @property
    def database(self):
        plain = Path(str(self))
        for candidate in (plain, *plain.parents):
            if candidate.name == "book" and (candidate / "novel.sqlite3").is_file():
                return BookDatabase(candidate)
        for candidate in (plain, *plain.parents):
            if candidate.name == "book":
                return BookDatabase(candidate)
        raise ValueError(f"not a book artifact: {self}")

    @property
    def key(self):
        return Path(str(self)).relative_to(self.database.book).as_posix()

    @property
    def managed(self):
        key = self.key
        if key in (".", "novel.sqlite3") or key.startswith("novel.sqlite3-"):
            return False
        if key.startswith("staging/") and self.name.startswith(("draft-", "polished-", "assembly-", "hatch_")):
            return self.database.has(key)
        if key.startswith("run/"):
            if self.name == "LOCK":
                return False
        return True

    def write_authoritative(self, body: bytes):
        if not self.managed:
            from .util import atomic_bytes
            atomic_bytes(Path(str(self)), body)
            return
        self.database.write(self.key, body)
        self.database.project(self.key, body)

    def append_authoritative(self, body: bytes):
        if not self.managed:
            from .util import append_bytes
            append_bytes(Path(str(self)), body)
            return
        self.database.append(self.key, body)
        self.database.project(self.key, self.database.read(self.key))

    def read_bytes(self):
        return self.database.read(self.key) if self.managed else Path(str(self)).read_bytes()

    def read_text(self, encoding=None, errors=None):
        return self.read_bytes().decode(encoding or "utf-8", errors or "strict")

    def write_bytes(self, data):
        self.write_authoritative(bytes(data))
        return len(data)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is not None:
            data = data.replace("\n", newline)
        self.write_bytes(data.encode(encoding or "utf-8", errors or "strict"))
        return len(data)

    def exists(self):
        if not self.managed:
            return Path(str(self)).exists()
        return self.database.has(self.key) or self.is_dir()

    def is_file(self):
        return self.database.has(self.key) if self.managed else Path(str(self)).is_file()

    def is_dir(self):
        return Path(str(self)).is_dir() or any(key.startswith(self.key.rstrip("/") + "/") for key in self.database.paths())

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        return Path(str(self)).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def unlink(self, missing_ok=False):
        if not self.managed:
            return Path(str(self)).unlink(missing_ok=missing_ok)
        if not self.database.has(self.key) and not missing_ok:
            raise FileNotFoundError(str(self))
        self.database.delete(self.key)
        self.database.project(self.key, None)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if not self.managed:
            return Path(str(self)).open(mode, buffering, encoding, errors, newline)
        return _DocumentBuffer(self, mode, encoding, errors, newline)

    def stat(self, *, follow_symlinks=True):
        if not self.managed or Path(str(self)).is_dir():
            return Path(str(self)).stat(follow_symlinks=follow_symlinks)
        size, ns = self.database.metadata(self.key)
        stamp = ns / 1_000_000_000
        return SimpleNamespace(st_mode=0o100644, st_size=size, st_mtime=stamp,
            st_mtime_ns=ns, st_atime=stamp, st_atime_ns=ns,
            st_ctime=stamp, st_ctime_ns=ns)

    def glob(self, pattern):
        # Directory discovery includes virtual documents even when exports were
        # deleted. Stray edits to an export never become authoritative input.
        prefix = "" if self.key == "." else self.key + "/"
        candidates = set()
        for key in self.database.paths():
            if not key.startswith(prefix):
                continue
            rel = key[len(prefix):]
            candidates.add(rel)
            parts = rel.split("/")
            for size in range(1, len(parts)):
                candidates.add("/".join(parts[:size]))
        for p in Path(str(self)).glob(pattern):
            wrapped = ArtifactPath(p)
            if p.is_dir() or not wrapped.managed:
                candidates.add(p.relative_to(Path(str(self))).as_posix())
        for rel in sorted(candidates):
            # pathlib's **/*.json includes files directly in the base folder.
            match = Path(rel).match(pattern)
            if pattern.startswith("**/"):
                match = match or Path(rel).match(pattern[3:])
            if match and ("/" in pattern or "/" not in rel):
                yield self / rel

    def rglob(self, pattern):
        return self.glob("**/" + pattern)

    def iterdir(self):
        return self.glob("*")
