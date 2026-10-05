from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


def coerce_due(value: Any) -> int | None:
    """due 字段的宽容归一：int 原样；「36」/「第 36 章」→ 36；无数字 → None。

    旧契约曾把 debts.due 文档化成「第 N 章」文本，毒值入账后 chapter next 的
    到期检查裸 int() 崩（实测）。所有读 due 的地方一律走本函数，
    不再裸 int()——归一只救可判读的值，不掩盖语义不明（None 由调用方按「无限期」
    或「忽略」处置）。放在纯函数层：views 声明不 import store，不能经由 ledger
    间接拉进 BookStore。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def now_ts() -> int:
    return int(time.time())


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def pack_hash_digest(payload: Any) -> str:
    return "sha256:" + sha256_bytes(canonical_json(payload))


def atomic_bytes(path: Path, data: bytes) -> None:
    if hasattr(path, "write_authoritative"):
        path.write_authoritative(data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, canonical_json(value))


def ordered_json(value: Any) -> bytes:
    """保序序列化：不排序键，按 dict 插入顺序落盘。

    只给**阶段视图**用（draft/polish/...）。canonical pack 与哈希仍走 `canonical_json`
    （排序键，字节确定），所以视图保序不影响任何 hash。视图保序的目的：把每章一字不差的
    稳定块（文风/内容手册）排在文件最前，形成尽可能长的**稳定前缀**，供宿主 prompt cache
    命中——`canonical_json` 排序键会把手册排到 `beats` 之后，前缀被逐章变化的拍点打断。
    """
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def atomic_json_ordered(path: Path, value: Any) -> None:
    atomic_bytes(path, ordered_json(value))


def atomic_text(path: Path, text: str) -> None:
    atomic_bytes(path, text.encode("utf-8"))


def append_bytes(path: Path, data: bytes) -> None:
    if hasattr(path, "append_authoritative"):
        path.append_authoritative(data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def stable_read_bytes(path: Path, *, attempts: int = 2, settle_seconds: float = 0.05) -> bytes:
    """submit 入口的读侧自证：读两次，字节一致才算拿到，不一致小睡后重试。

    防两类撕裂：worker 还在写文件时宿主开始读；投影层正在追赶真源时被读到半程。
    仍不一致说明有并发写者在跑，交 torn_read 让调用方决策，不拿可疑字节过闸门。
    """
    import time

    last: bytes | None = None
    for attempt in range(max(1, attempts)):
        first = path.read_bytes()
        if first == last:
            return first
        last = first
        if attempt + 1 < max(1, attempts):
            time.sleep(settle_seconds)
    if attempts <= 1:
        return last or path.read_bytes()
    raise LedgerError(
        "torn_read",
        f"file kept changing while being read (concurrent writer?): {path}",
        {"path": str(path), "attempts": attempts},
    )


def stable_read_text(path: Path, encoding: str = "utf-8-sig") -> str:
    # utf-8-sig 兼容无 BOM 文件，只在首位有 BOM 时剥掉：Windows worker 用
    # PowerShell 5.1 默认写 UTF-8 BOM，正文/JSON 首字符带 \ufeff 会炸解析或
    # 混进引文比对（实测 ack-read --quotes-file invalid_json 一轮白损）。
    return stable_read_bytes(path).decode(encoding)


def stable_read_json(path: Path) -> Any:
    return json.loads(stable_read_bytes(path).decode("utf-8-sig"))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError("invalid_json", f"cannot read JSON: {path}", str(exc)) from exc


def chinese_word_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))


def ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}
