"""SQLite persistence and rebuildable relational projections for one book.

Documents preserve the existing serialization contracts. All authoritative
reads use SQLite; files are exports or unsubmitted worker inputs. Query tables
are maintained in the same transaction as their source documents.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from .util import LedgerError

SCHEMA_VERSION = 1
_ACTIVE = contextvars.ContextVar("novel_sqlite_transactions", default={})
_EXPORTS = contextvars.ContextVar("novel_sqlite_exports", default={})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 path TEXT PRIMARY KEY, body BLOB NOT NULL, revision INTEGER NOT NULL,
 hash TEXT NOT NULL, updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 path TEXT NOT NULL, revision INTEGER NOT NULL, body BLOB NOT NULL,
 hash TEXT NOT NULL, updated_ns INTEGER NOT NULL, PRIMARY KEY(path, revision)
);
CREATE TABLE IF NOT EXISTS chapters (
 chapter INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
 prose_hash TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, chapter INTEGER NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_chapter ON events(chapter);
CREATE TABLE IF NOT EXISTS event_entities (
 event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 entity TEXT NOT NULL, PRIMARY KEY(event_id, entity)
);
CREATE INDEX IF NOT EXISTS event_entities_name ON event_entities(entity, event_id);
CREATE TABLE IF NOT EXISTS memories (
 id TEXT PRIMARY KEY, event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
 chapter INTEGER NOT NULL, kind TEXT NOT NULL, who TEXT NOT NULL,
 target TEXT NOT NULL, topic TEXT NOT NULL, asset TEXT NOT NULL,
 text TEXT NOT NULL, evidence TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_person ON memories(who, chapter);
CREATE INDEX IF NOT EXISTS memories_topic ON memories(topic, chapter);
CREATE INDEX IF NOT EXISTS memories_asset ON memories(asset, chapter);
CREATE TABLE IF NOT EXISTS passages (
 id INTEGER PRIMARY KEY, chapter INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 text TEXT NOT NULL, prose_hash TEXT NOT NULL,
 UNIQUE(chapter, ordinal)
);
CREATE INDEX IF NOT EXISTS passages_chapter ON passages(chapter);
"""


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class BookDatabase:
    def __init__(self, book: Path):
        self.book = Path(book)
        self.path = self.book / "novel.sqlite3"

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if write:
            self.book.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5)
        else:
            if not self.exists():
                raise LedgerError("not_initialized", f"missing database: {self.path}")
            uri = "file:" + quote(str(self.path.resolve()), safe="/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # 测试加速通道：千余个用例各自建库 + 章节循环里的小事务在默认 fsync 下是
        # 套件时长的大头。测试不需要崩溃耐久（真源在事件链 + git）， opting in 的
        # 环境变量由 conftest 统一设置；生产环境不设该变量，语义不变。
        if os.environ.get("NOVEL_LEDGER_TEST_FAST_DB") == "1":
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION) or (version == 0 and not write):
            conn.close()
            raise LedgerError("unsupported_database_schema", f"unsupported SQLite schema: {version}")
        if write and version == 0:
            conn.executescript(_SCHEMA)
            # Trigram supports Chinese substrings without an external tokenizer.
            # Small queries and runtimes without FTS5 use bounded substring search.
            try:
                conn.execute("CREATE VIRTUAL TABLE passage_fts USING fts5(text, content='passages', content_rowid='id', tokenize='trigram')")
                conn.executescript("""
                CREATE TRIGGER passage_insert AFTER INSERT ON passages BEGIN
                  INSERT INTO passage_fts(rowid,text) VALUES(new.id,new.text);
                END;
                CREATE TRIGGER passage_delete AFTER DELETE ON passages BEGIN
                  INSERT INTO passage_fts(passage_fts,rowid,text) VALUES('delete',old.id,old.text);
                END;
                """)
            except sqlite3.OperationalError:
                pass
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        return conn

    @contextlib.contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        key = str(self.path.resolve())
        active = _ACTIVE.get().get(key)
        if active is not None:
            yield active
            return
        conn = self._connect(write=write)
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        key = str(self.path.resolve())
        if key in _ACTIVE.get():
            yield
            return
        pending = {}
        export_token = _EXPORTS.set({**_EXPORTS.get(), key: pending})
        try:
            with self.connection(write=True) as conn:
                token = _ACTIVE.set({**_ACTIVE.get(), key: conn})
                try:
                    yield
                finally:
                    _ACTIVE.reset(token)
            for path, body in pending.items():
                self._project_file(path, body)
        finally:
            _EXPORTS.reset(export_token)

    def project(self, path: str, body: bytes | None) -> None:
        pending = _EXPORTS.get().get(str(self.path.resolve()))
        if pending is not None:
            pending[path] = body
        else:
            self._project_file(path, body)

    def _project_file(self, path: str, body: bytes | None) -> None:
        file = self.book / path
        if body is None:
            file.unlink(missing_ok=True)
        else:
            from .util import atomic_bytes
            atomic_bytes(file, body)

    def read(self, path: str) -> bytes:
        if not self.exists():
            raise FileNotFoundError(path)
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM documents WHERE path=?", (path,)).fetchone()
        if row is None:
            raise FileNotFoundError(path)
        return bytes(row[0])

    def has(self, path: str) -> bool:
        if not self.exists():
            return False
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM documents WHERE path=?", (path,)).fetchone() is not None

    def paths(self) -> list[str]:
        if not self.exists():
            return []
        with self.connection() as conn:
            return [row[0] for row in conn.execute("SELECT path FROM documents ORDER BY path")]

    def metadata(self, path: str):
        with self.connection() as conn:
            row = conn.execute("SELECT length(body), updated_ns FROM documents WHERE path=?", (path,)).fetchone()
        if row is None:
            raise FileNotFoundError(path)
        return row

    def write(self, path: str, body: bytes) -> None:
        with self.connection(write=True) as conn:
            previous = conn.execute("SELECT revision, body FROM documents WHERE path=?", (path,)).fetchone()
            if previous is not None and bytes(previous[1]) == body:
                return
            latest = conn.execute("SELECT max(revision) FROM revisions WHERE path=?", (path,)).fetchone()[0]
            revision = max(int(previous[0]) if previous else 0, int(latest or 0)) + 1
            digest, stamp = _digest(body), time.time_ns()
            conn.execute("INSERT INTO documents VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET body=excluded.body,revision=excluded.revision,hash=excluded.hash,updated_ns=excluded.updated_ns", (path, body, revision, digest, stamp))
            if path.startswith("chapters/") and path.endswith(".md"):
                conn.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (path, revision, body, digest, stamp))
            self._project(conn, path, body, revision, digest)

    def append(self, path: str, body: bytes) -> None:
        with self.transaction():
            old = self.read(path) if self.has(path) else b""
            self.write(path, old + body)

    def delete(self, path: str) -> None:
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT chapter FROM chapters WHERE path=?", (path,)).fetchone()
            if row:
                conn.execute("DELETE FROM chapters WHERE path=?", (path,))
                conn.execute("DELETE FROM passages WHERE chapter=?", (row[0],))
            if path == "ledger/events.jsonl":
                conn.execute("DELETE FROM events")
            if path.startswith("summaries/l1/"):
                conn.execute("DELETE FROM memories WHERE id=?", ("summary:" + path,))
            conn.execute("DELETE FROM documents WHERE path=?", (path,))

    def _project(self, conn, path: str, body: bytes, revision: int, digest: str) -> None:
        if path.startswith("chapters/") and path.endswith(".md"):
            try:
                chapter = int(Path(path).stem.split("-")[-1])
                prose = body.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return
            conn.execute("INSERT INTO chapters VALUES(?,?,?,?) ON CONFLICT(chapter) DO UPDATE SET path=excluded.path,prose_hash=excluded.prose_hash,revision=excluded.revision", (chapter, path, digest, revision))
            conn.execute("DELETE FROM passages WHERE chapter=?", (chapter,))
            for ordinal, text in enumerate(prose.splitlines()):
                if text.strip() and not text.startswith("#"):
                    conn.execute("INSERT INTO passages(chapter,ordinal,text,prose_hash) VALUES(?,?,?,?)", (chapter, ordinal, text, digest))
        elif path == "ledger/events.jsonl":
            self._project_events(conn, body)
        elif path.startswith("summaries/l1/") and path.endswith(".json"):
            ident = "summary:" + path
            conn.execute("DELETE FROM memories WHERE id=?", (ident,))
            try:
                raw = json.loads(body)
                self._memory(conn, ident, None, int(raw["chapter"]), "summary", {"text": raw["l1_summary"]})
            except (ValueError, KeyError, TypeError):
                pass

    def _project_events(self, conn, body: bytes) -> None:
        # Invalid journals must remain visible to ledger verify, never repaired
        # by indexing. Remove derived rows so recall cannot use stale evidence.
        try:
            values = [json.loads(line) for line in body.splitlines() if line.strip()]
            if any(not isinstance(e, dict) for e in values):
                raise ValueError("non-object event")
        except (ValueError, UnicodeDecodeError):
            conn.execute("DELETE FROM events")
            return
        existing = {row[0]: row[1] for row in conn.execute("SELECT seq,payload FROM events")}
        encoded_values = [json.dumps(event, ensure_ascii=False, sort_keys=True) for event in values]
        changed = next((seq for seq, encoded in enumerate(encoded_values) if existing.get(seq) != encoded), len(values))
        conn.execute("DELETE FROM events WHERE seq>=?", (changed,))
        seen = {row[0] for row in conn.execute("SELECT id FROM events")}
        for seq, event in enumerate(values[changed:], start=changed):
            encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
            conn.execute("DELETE FROM events WHERE seq=?", (seq,))
            ident = str(event.get("hash") or f"event:{seq}")
            if ident in seen:
                # Keep malformed source bytes for verify; distinguish duplicate
                # rows so indexing cannot mask a duplicate chapter event.
                ident += f":duplicate:{seq}"
            seen.add(ident)
            chapter = int(event.get("chapter") or event.get("effective_chapter") or 0)
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (ident, seq, chapter, str(event.get("type") or "chapter"), encoded))
            delta = event.get("state_delta") or {}
            self._memory(conn, ident + ":event", ident, chapter, "event", {"text": str(event.get("l1_summary") or f"第{chapter}章登记事件")})
            actors = set(str(n) for n in delta.get("named", []) if isinstance(n, str))
            for kind, records in delta.items():
                if not isinstance(records, list):
                    continue
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        continue
                    for key in ("who", "target", "holder", "from", "to", "creditor", "debtor"):
                        value = record.get(key)
                        if isinstance(value, str) and value:
                            actors.add(value)
                    self._memory(conn, f"{ident}:{kind}:{index}", ident, chapter, kind, record)
            for actor in actors:
                conn.execute("INSERT OR IGNORE INTO event_entities VALUES(?,?)", (ident, actor))
            if event.get("type") == "governance":
                # Rulings have top-level selectors rather than state_delta.
                # Project these too so person/asset recall can find the later
                # closure or rename instead of returning only the old promise.
                governed_actors = {
                    event[key] for key in ("who", "target", "from_name", "to_name")
                    if isinstance(event.get(key), str) and event[key].strip()
                }
                governed_assets = list(dict.fromkeys(
                    event[key] for key in ("hook_id", "into_id")
                    if isinstance(event.get(key), str) and event[key].strip()
                ))
                for asset in governed_assets:
                    governed_actors.update(row[0] for row in conn.execute(
                        "SELECT DISTINCT ee.entity FROM event_entities ee "
                        "JOIN memories m ON m.event_id=ee.event_id WHERE m.asset=?", (asset,),
                    ))
                for actor in governed_actors:
                    conn.execute("INSERT OR IGNORE INTO event_entities VALUES(?,?)", (ident, actor))
                self._memory(conn, ident + ":governance", ident, chapter, "governance", event)
                for asset in governed_assets[1:]:
                    self._memory(conn, ident + ":governance:" + asset, ident, chapter, "governance", {**event, "id": asset})
        conn.execute("DELETE FROM events WHERE seq>=?", (len(values),))

    @staticmethod
    def _memory(conn, ident, event_id, chapter, kind, record) -> None:
        text = str(record.get("text") or record.get("claim") or record.get("hint") or record.get("reason") or "")
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        conn.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident, event_id, chapter, kind, str(record.get("who") or record.get("holder") or ""), str(record.get("target") or ""), str(record.get("topic_id") or ""), str(record.get("id") or record.get("item_id") or record.get("hook_id") or record.get("into_id") or ""), text or encoded, str(record.get("quote") or ""), encoded))

    def export(self, *, destination: Path | None = None) -> int:
        target = Path(destination) if destination else self.book
        with self.connection() as conn:
            rows = list(conn.execute("SELECT path,body FROM documents ORDER BY path"))
        for path, body in rows:
            file = target / path
            from .util import atomic_bytes
            atomic_bytes(file, bytes(body))
        return len(rows)

    def backup(self, destination: Path) -> dict:
        target = Path(destination).resolve()
        if target == self.path.resolve() or target.exists():
            raise LedgerError("invalid_backup_destination", "backup requires a new destination file")
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(target.name + ".pending")
        if pending.exists():
            raise LedgerError("invalid_backup_destination", "backup temporary file already exists")
        try:
            with self.connection() as source:
                dest = sqlite3.connect(str(pending))
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            import os
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
        return {"ok": True, "action": "database_backup", "destination": str(target)}

    def integrity(self) -> dict:
        with self.connection() as conn:
            check = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            foreign = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
            hashes = [row["path"] for row in conn.execute("SELECT path,body,hash FROM documents") if _digest(bytes(row["body"])) != row["hash"]]
            counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("documents", "chapters", "events", "memories", "passages")}
        return {"ok": check == ["ok"] and not foreign and not hashes, "checks": check, "foreign_key_issues": foreign, "document_hash_issues": hashes, "counts": counts, "database_path": str(self.path)}
