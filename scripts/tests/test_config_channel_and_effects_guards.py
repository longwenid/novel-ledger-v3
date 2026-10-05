"""配置正规写通道 + effects 稳定键防线的两个架构修复回归。

- items 键形检查曾是不可达死代码（`required is None → continue` 先于 items 特判），
  只写 `item` 键的条目经 plan extend 静默入盘，到组装提交才以 expected_delta_missing
  爆出（真实项目烧掉整夜一轮写链）。本文件锁死：items 必须 id 或 name 至少其一，
  与 content/gates.py _find_actual 的 items 分支对齐。
- config 的真源是 sqlite documents 表，磁盘 config.json 只是投影；散改不生效也无提示
  （曾整夜误诊为缓存/读抖动）。`config set` 是唯一正规写通道，status 检出影子漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.cli import (
    _dotted_config_get,
    _dotted_config_set,
    _parse_config_value,
)
from novel_ledger_core.control.pipeline import status
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.story_map import must_word_issues, plan_effects_key_issues, plan_shape_reject
from novel_ledger_core.infra.util import LedgerError


def _items_entry(**overrides) -> dict:
    entry = {"name": "止血草", "owner": "张三", "status": "held"}
    entry.update(overrides)
    return {k: v for k, v in entry.items() if v is not None}


def _chapter_with_effects(kind: str, entry: dict) -> list[dict]:
    return [{"chapter": 9, "beats": [{"id": "b2", "effects": {kind: [entry]}}]}]


class TestItemsStableKeys:
    def test_item_key_rejected_name_or_id_required(self):
        # 昨夜真实事故形状：只写 `item`，匹配器永不认（曾有特判但不可达）。
        issues = plan_effects_key_issues(_chapter_with_effects("items", {
            "item": "止血草", "owner": "张三", "status": "held",
        }))
        assert issues and issues[0]["kind"] == "items"
        assert issues[0]["missing_keys"] == ["id or name"]
        assert "item" in issues[0]["entry_keys"]

    def test_name_only_and_id_only_both_accepted(self):
        assert plan_effects_key_issues(_chapter_with_effects("items", _items_entry(id=None))) == []
        assert plan_effects_key_issues(_chapter_with_effects("items", _items_entry(name=None, id="item-1"))) == []

    def test_blank_name_and_id_rejected(self):
        issues = plan_effects_key_issues(_chapter_with_effects("items", _items_entry(name="  ", id="")))
        assert issues and issues[0]["missing_keys"] == ["id or name"]

    def test_conditions_cond_status_shape_still_rejected(self):
        issues = plan_effects_key_issues(_chapter_with_effects("conditions", {
            "cond": "疫情", "status": "open",
        }))
        assert issues and issues[0]["missing_keys"] == ["who", "text"]


class TestEffectsGuardEntrypoints:
    def test_reject_helper_raises_with_origin(self):
        with pytest.raises(LedgerError) as err:
            plan_shape_reject(
                _chapter_with_effects("items", {"item": "止血草"}),
                origin="plan extend",
            )
        assert err.value.code == "invalid_plan"

    def test_init_project_rejects_bad_effects(self, tmp_path: Path):
        # hatch 初签章拍（step6_beats）经 init_project 入盘——必须与 extend 同一道防线。
        plan = {
            "title": "t", "protagonist": "张三", "volume_spine": "",
            "chapters": _chapter_with_effects("conditions", {"cond": "x", "status": "open"}),
        }
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        proj = tmp_path / "p"
        proj.mkdir()
        with pytest.raises(LedgerError) as err:
            init_project(proj, plan_path=plan_path, protagonist="张三")
        assert err.value.code == "invalid_plan"

    def test_extend_plan_rejects_items_item_key(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        with pytest.raises(LedgerError) as err:
            store.extend_plan(_chapter_with_effects("items", {"item": "止血草"}))
        assert err.value.code == "invalid_plan"


def _initialized_store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "t", "protagonist": "张三", "volume_spine": "",
        "chapters": [{
            "chapter": 1, "volume": 1, "location": "a", "present": ["张三"], "tags": [],
            "beats": [{"id": "b1", "required": True, "text": "x", "must": "x"}],
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="张三")
    return BookStore(proj)


class TestConfigChannel:
    def test_authoritative_set_visible_through_load(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        cfg = store.load_config()
        assert cfg["polish"] == "off"  # skill 默认只写作
        _dotted_config_set(cfg, "polish", _parse_config_value("off"))
        store.save_config(cfg)
        assert BookStore(tmp_path / "proj").load_config()["polish"] == "off"

    def test_shadow_edit_never_authoritative_and_surfaced(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        assert store.config_shadow_divergence() is None
        plain = Path(str(store.config_path))
        plain.write_text(
            plain.read_text(encoding="utf-8").replace('"polish":"off"', '"polish":"on"'),
            encoding="utf-8",
        )
        # 数据库真源不因文件散改而变——这正是曾把整夜排查引向缓存误诊的点。
        assert BookStore(tmp_path / "proj").load_config()["polish"] == "off"
        drift = store.config_shadow_divergence()
        assert drift and drift["file_hash"] != drift["database_hash"]
        assert "config set" in drift["hint"]
        # status 必须把漂移带出来，不能无声。
        assert status(store)["config_shadow"] is not None

    def test_status_reports_null_when_clean(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        assert status(store)["config_shadow"] is None

    def test_set_rejects_unknown_keys(self, tmp_path: Path):
        cfg = _initialized_store(tmp_path).load_config()
        with pytest.raises(LedgerError) as err:
            _dotted_config_set(cfg, "polsh", "off")
        assert err.value.code == "unknown_config_key"
        with pytest.raises(LedgerError):
            _dotted_config_set(cfg, "word_band.not_a_knob", 1)

    def test_value_parsing(self):
        assert _parse_config_value("off") == "off"
        assert _parse_config_value("2500") == 2500
        assert _parse_config_value("true") is True
        assert _parse_config_value("[1,2]") == [1, 2]

    def test_dotted_get_nested(self, tmp_path: Path):
        cfg = _initialized_store(tmp_path).load_config()
        assert _dotted_config_get(cfg, "word_band.min") == cfg["word_band"]["min"]
        with pytest.raises(LedgerError):
            _dotted_config_get(cfg, "no.such.key")


class TestMustInBeatGuard:
    def test_must_not_in_beat_rejected_at_write(self):
        bad = [{"chapter": 7, "beats": [{"id": "b1", "text": "主角在市集拒收改期", "must": "根本不在拍里的词"}]}]
        assert must_word_issues(bad)[0]["must"] == "根本不在拍里的词"
        with pytest.raises(LedgerError):
            plan_shape_reject(bad, origin="plan extend")

    def test_must_in_beat_passes(self):
        good = [{"chapter": 7, "beats": [{"id": "b1", "text": "主角在市集拒收改期", "must": "拒收"}]}]
        assert must_word_issues(good) == []
        plan_shape_reject(good, origin="plan extend")  # 不抛


class TestDocumentShadow:
    def test_clean_project_reports_none(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        assert store.document_shadow_divergence() is None
        assert status(store)["document_shadow"] is None

    def test_disk_edited_and_host_only_both_surfaced(self, tmp_path: Path):
        store = _initialized_store(tmp_path)
        book = tmp_path / "proj" / "book"
        plan_file = book / "plan" / "chapters.json"
        plan_file.write_text(plan_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
        (book / "pack" / "host-only.json").write_text("{}", encoding="utf-8")
        drift = store.document_shadow_divergence()
        assert drift is not None
        assert "plan/chapters.json" in drift["disk_edited"]
        assert "pack/host-only.json" in drift["host_only"]
        assert "database" in drift["hint"]
        # 真源不因散改投影而变——教训6 的核心：控制面读的仍是数据库。
        assert store.load_plan()["protagonist"] == "张三"


class TestStableRead:
    def test_stable_read_passes_on_quiet_file(self, tmp_path: Path):
        from novel_ledger_core.infra.util import stable_read_bytes
        f = tmp_path / "a.txt"
        f.write_text("稳定内容", encoding="utf-8")
        assert stable_read_bytes(f) == "稳定内容".encode("utf-8")

    def test_stable_read_raises_on_concurrent_writer(self, tmp_path: Path, monkeypatch):
        import novel_ledger_core.infra.util as util
        from novel_ledger_core.infra.util import stable_read_bytes, LedgerError
        f = tmp_path / "a.txt"
        f.write_text("第一次", encoding="utf-8")
        calls = {"n": 0}
        real = Path.read_bytes
        def flaky(self, *a, **k):
            calls["n"] += 1
            return f"第{calls['n']}次".encode("utf-8")
        monkeypatch.setattr(Path, "read_bytes", flaky)
        with pytest.raises(LedgerError) as err:
            stable_read_bytes(f, settle_seconds=0)
        assert err.value.code == "torn_read"
