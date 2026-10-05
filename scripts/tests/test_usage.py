from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import _usage_from_cli, main
from novel_ledger_core.control.pipeline import audit_book, chapter_next, record_usage, status, void_usage
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError


def _init(tmp_path: Path, name: str = "book") -> BookStore:
    project = tmp_path / name
    project.mkdir()
    plan = tmp_path / f"{name}-plan.json"
    plan.write_text(json.dumps({
        "protagonist": "主角",
        "chapters": [{
            "chapter": 1,
            "location": "市集",
            "present": ["主角"],
            "beats": [{"id": "b1", "required": True, "text": "主角进入市集", "must": "市集"}],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    init_project(project, plan_path=plan, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def _sample(uncached: int, *, read: int = 0, write: int = 0, output: int = 0) -> dict:
    return {
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_write_input_tokens": write,
        "output_tokens": output,
    }


def test_usage_is_missing_without_records(tmp_path: Path):
    usage = status(_init(tmp_path))["usage"]
    assert usage["telemetry"] == "missing"
    assert usage["records"] == 0


def test_usage_v3_deltas_aggregate_retries_and_cache(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="r1", usage=_sample(10, read=20, output=2))
    record_usage(store, chapter=1, stage="draft", request_id="r2", usage=_sample(5, write=5, output=3))
    usage = status(store)["usage"]
    assert usage["records"] == 2
    assert usage["effective_input_tokens"] == 40
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 45
    assert usage["cache_hit_ratio"] == 0.625


def test_usage_bad_or_legacy_lines_are_ignored_and_counted(tmp_path: Path):
    store = _init(tmp_path)
    store.usage_path.write_text(
        'not json\n{"chapter":1,"input_tokens":99,"output_tokens":1}\n',
        encoding="utf-8",
    )
    usage = status(store)["usage"]
    assert usage["records"] == 0
    assert usage["bad_lines"] == 2


def test_status_and_audit_expose_same_usage(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="r1", usage=_sample(3, output=4))
    assert status(store)["usage"]["total_tokens"] == 7
    assert audit_book(store)["usage_summary"]["total_tokens"] == 7


def test_usage_record_is_idempotent_and_conflicts_fail(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    first = record_usage(store, chapter=1, stage="draft", request_id="same", usage=_sample(7))
    again = record_usage(store, chapter=1, stage="draft", request_id="same", usage=_sample(7))
    assert first["duplicate"] is False
    assert again["duplicate"] is True
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=1, stage="draft", request_id="same", usage=_sample(8))
    assert exc.value.code == "usage_request_conflict"


def test_usage_record_requires_explicit_nonnegative_components(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=1, stage="draft", request_id="legacy", usage={"input_tokens": 5})
    assert exc.value.code == "invalid_usage"
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=1, stage="draft", request_id="negative", usage=_sample(-1))
    assert exc.value.code == "invalid_usage"
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=1, stage="draft", request_id="missing", usage={"output_tokens": 1})
    assert exc.value.code == "invalid_usage"


def test_usage_record_requires_current_action_chapter(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=None, stage="draft", request_id="none", usage=_sample(1))
    assert exc.value.code == "missing_chapter"
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=2, stage="draft", request_id="wrong", usage=_sample(1))
    assert exc.value.code == "usage_chapter_mismatch"


def test_usage_budget_stops_next_model_action(tmp_path: Path):
    store = _init(tmp_path)
    cfg = store.load_config()
    cfg["usage_budget"]["stop_input_per_chapter"] = 10
    store.save_config(cfg)
    chapter_next(store)
    result = record_usage(store, chapter=1, stage="draft", request_id="over", usage=_sample(10))
    assert result["stop"] is True
    guarded = chapter_next(store)
    assert guarded["action"] == "usage_guard"
    assert guarded["reasons"] == ["chapter_input_tokens"]


def test_usage_record_cli_roundtrip(tmp_path: Path, capsys):
    store = _init(tmp_path)
    chapter_next(store)
    code = main([
        "chapter", "usage-record", "--project", str(store.project),
        "--chapter", "1", "--stage", "draft", "--request-id", "cli-1",
        "--uncached-input-tokens", "4", "--cache-read-input-tokens", "6",
        "--output-tokens", "2",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["sample"]["schema"] == "novel-ledger.usage.v3"
    assert payload["chapter_usage"]["effective_input_tokens"] == 10


def test_usage_cli_parser_has_one_unambiguous_shape():
    args = argparse.Namespace(
        uncached_input_tokens=7,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
        output_tokens=1,
    )
    assert _usage_from_cli(args) == _sample(7, read=3, write=2, output=1)


def test_status_usage_detail_is_windowed_on_long_books(tmp_path: Path):
    """status/book audit 的 usage 明细只回最近窗口——长跑中每个 worker 会话都要读 status,
    全量 by_chapter 随章数线性膨胀(千章实测 status 223KB / book audit 237KB),
    是随请求反复重发的二次方 token 浪费。汇总量必须保持全书口径。"""
    store = _init(tmp_path)
    # 直写 usage.jsonl 真源（record_usage 有章号防呆，只允许记录当前动作章——这里要
    # 造 20 章的历史明细来测汇总层窗口）
    lines = []
    for ch in range(1, 21):
        lines.append(json.dumps({
            "schema": "novel-ledger.usage.v3", "chapter": ch, "stage": "draft",
            "request_id": f"r{ch}", "session_id": f"s{ch}",
            "effective_input_tokens": 100 + ch, "uncached_input_tokens": 100 + ch,
            "cache_read_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 10,
        }, ensure_ascii=False))
    with store.usage_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    s = status(store)
    usage = s["usage"]
    assert len(usage["by_chapter"]) == 12
    assert usage["chapters"] == list(range(9, 21)), "窗口必须是最近的 12 章"
    assert usage["chapters_total"] == 20
    assert usage["detail_window"] == 12
    # 汇总量仍是全书口径：窗口里只有 12 章的明细，totals 覆盖 20 章
    window_output = sum(v["output_tokens"] for v in usage["by_chapter"].values())
    assert window_output == 120
    assert usage["output_tokens"] == 200
    assert usage["max_input_per_chapter"] == 120  # 第 20 章的 100+20

    # book audit 同样开窗
    audit = audit_book(store)
    assert len(audit["usage_summary"]["by_chapter"]) == 12
    assert audit["usage_summary"]["chapters_total"] == 20


def test_status_usage_detail_full_below_window(tmp_path: Path):
    """小于窗口的书不截断、无新标记——旧行为与旧消费方零感知。"""
    store = _init(tmp_path)
    record_usage(store, chapter=1, stage="draft", request_id="r1", usage=_sample(10, output=2))
    usage = status(store)["usage"]
    assert usage["by_chapter"].keys() == {"1"}
    assert "chapters_total" not in usage
    assert "detail_window" not in usage


def test_usage_guard_still_sees_full_summary_outside_window(tmp_path: Path):
    """进程内单章查询（usage_guard / usage-record）不受输出窗口影响：
    暂停很久后续跑旧章时，该章用量明细必须仍可被熔断器查到。"""
    store = _init(tmp_path)
    lines = []
    for ch in range(1, 21):
        lines.append(json.dumps({
            "schema": "novel-ledger.usage.v3", "chapter": ch, "stage": "draft",
            "request_id": f"r{ch}", "session_id": f"s{ch}",
            "effective_input_tokens": 900_000, "uncached_input_tokens": 900_000,
            "cache_read_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 10,
        }, ensure_ascii=False))
    with store.usage_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    head = store.read_head()
    head["chapter"] = 3
    from novel_ledger_core.control.pipeline import _usage_guard_action
    guard = _usage_guard_action(store, head, candidate_chapter=3)
    assert guard is not None, "第 3 章在窗口外，但熔断器必须仍能查到它的用量"
    assert guard["action"] == "usage_guard"


def test_usage_total_only_samples_aggregate_and_drive_total_stop(tmp_path: Path):
    """stage-agent 子代理宿主只见整单总量：total-only 是合法形状，
    计入章节总量并驱动 stop_total_per_chapter 熔断——telemetry 不再恒 unknown。"""
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="t1", usage={"total_tokens": 1000})
    record_usage(store, chapter=1, stage="assemble", request_id="t2", usage={"total_tokens": 1500})
    usage = status(store)["usage"]
    assert usage["records"] == 2
    assert usage["total_tokens"] == 2500
    assert usage["effective_input_tokens"] == 0
    assert usage["by_chapter"]["1"]["total_only_tokens"] == 2500

    cfg = store.load_config()
    cfg["usage_budget"]["stop_total_per_chapter"] = 3000
    store.save_config(cfg)
    result = record_usage(store, chapter=1, stage="ack", request_id="t3", usage={"total_tokens": 600})
    assert result["stop"] is True
    assert result["reasons"] == ["chapter_total_tokens"]
    guarded = chapter_next(store)
    assert guarded["action"] == "usage_guard"
    assert "chapter_total_tokens" in guarded["reasons"]


def test_usage_total_only_request_id_is_idempotent_and_conflicts_fail(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    first = record_usage(store, chapter=1, stage="draft", request_id="dup", usage={"total_tokens": 900})
    assert first["duplicate"] is False
    again = record_usage(store, chapter=1, stage="draft", request_id="dup", usage={"total_tokens": 900})
    assert again["duplicate"] is True
    with pytest.raises(LedgerError) as exc:
        record_usage(store, chapter=1, stage="draft", request_id="dup", usage={"total_tokens": 901})
    assert exc.value.code == "usage_request_conflict"


def test_usage_rejects_mixed_shapes_and_nonpositive_total(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    with pytest.raises(LedgerError) as mixed:
        record_usage(
            store, chapter=1, stage="draft", request_id="m1",
            usage={"uncached_input_tokens": 5, "total_tokens": 10},
        )
    assert mixed.value.code == "invalid_usage"
    with pytest.raises(LedgerError) as zero:
        record_usage(store, chapter=1, stage="draft", request_id="m2", usage={"total_tokens": 0})
    assert zero.value.code == "invalid_usage"
    with pytest.raises(LedgerError) as empty:
        record_usage(store, chapter=1, stage="draft", request_id="m3", usage={})
    assert empty.value.code == "missing_usage"


def test_usage_record_cli_total_tokens_roundtrip(tmp_path: Path, capsys):
    store = _init(tmp_path)
    chapter_next(store)
    code = main([
        "chapter", "usage-record", "--project", str(store.project),
        "--chapter", "1", "--stage", "draft", "--request-id", "cli-total",
        "--total-tokens", "1234",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["sample"]["schema"] == "novel-ledger.usage.total.v1"
    assert payload["sample"]["total_tokens"] == 1234
    assert payload["chapter_usage"]["total_tokens"] == 1234
    assert payload["chapter_usage"]["total_only_tokens"] == 1234


def test_usage_void_excludes_misrecorded_sample(tmp_path: Path):
    """换 ID 重记同一笔时幂等拦不住（实测一章多记 15.7 万 tokens 噪音）：
    usage-void 作废后聚合口径必须剔除被作废样本。"""
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="r1", usage=_sample(10, read=20, output=2))
    record_usage(store, chapter=1, stage="draft", request_id="r2", usage=_sample(5, output=3))

    resp = void_usage(store, request_id="r1", reason="duplicate recorded under wrong request-id")
    assert resp["ok"] is True and resp["duplicate"] is False
    assert resp["voided_sample"]["effective_input_tokens"] == 30

    usage = status(store)["usage"]
    assert usage["records"] == 1
    assert usage["voided"] == 1
    assert usage["effective_input_tokens"] == 5
    assert usage["total_tokens"] == 8


def test_usage_void_is_idempotent_and_validated(tmp_path: Path):
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="v1", usage=_sample(9))
    assert void_usage(store, request_id="v1", reason="x")["duplicate"] is False
    again = void_usage(store, request_id="v1", reason="x")
    assert again["ok"] is True and again["duplicate"] is True
    with pytest.raises(LedgerError) as missing:
        void_usage(store, request_id="nope", reason="x")
    assert missing.value.code == "usage_request_not_found"


def test_usage_void_cli_roundtrip(tmp_path: Path, capsys):
    store = _init(tmp_path)
    chapter_next(store)
    record_usage(store, chapter=1, stage="draft", request_id="cli-v", usage={"total_tokens": 77})
    code = main([
        "chapter", "usage-void", "--project", str(store.project),
        "--request-id", "cli-v", "--reason", "wrong request-id",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["voided_sample"]["total_tokens"] == 77
    usage = status(store)["usage"]
    assert usage["records"] == 0
    assert usage["voided"] == 1
    assert usage["telemetry"] == "missing"
