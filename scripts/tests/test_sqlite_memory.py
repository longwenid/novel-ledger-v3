from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.content.recall import recall
from novel_ledger_core.content.views import make_draft_view, make_polish_view, make_assemble_view
from novel_ledger_core.control.cli import main
from novel_ledger_core.control.database_ops import migrate
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, canonical_json
from novel_ledger_core.ledger.ledger import commit_event, rollback_ledger_to
from tests.test_autopilot import _store


def history(store, chapter, *, who="甲", target="乙", text="两人当年约定保守秘密", quote="两人当年约定保守秘密", topic="旧约", asset="旧信"):
    prose = f"{quote}。\n旧信藏在柜底，至今没有交出去。\n"
    store.chapter_md_path(chapter).write_text(prose, encoding="utf-8")
    commit_event(store, chapter, {
        "named": [who, target], "facts": [{"who": who, "text": text, "quote": quote}],
        "knowledge": [{"who": who, "topic_id": topic, "claim": text, "stance": "knows", "source": "亲见", "quote": quote}],
        "relations": [{"who": who, "target": target, "kind": "旧交", "quote": quote}],
        "items": [{"id": asset, "holder": who, "text": "旧信仍在", "quote": "旧信藏在柜底"}],
    })
    head = store.read_head()
    head.update(chapter=chapter, last_committed_ch=chapter, last_acked_ch=chapter)
    store.write_head(head)
    return prose


def test_database_is_authoritative_after_exports_deleted_or_tampered(tmp_path):
    store = _store(tmp_path, chapters=2)
    prose = history(store, 1)
    original = store.load_config()
    Path(str(store.config_path)).write_text('{"protagonist":"伪造"}', encoding="utf-8")
    Path(str(store.chapter_md_path(1))).unlink()
    Path(str(store.head_path)).unlink()
    reopened = BookStore(store.project)
    assert reopened.load_config() == original
    assert reopened.read_head()["last_committed_ch"] == 1
    assert reopened.chapter_md_path(1).read_text(encoding="utf-8") == prose
    assert list(reopened.chapters_dir.glob("*/ch-*.md")) == [reopened.chapter_md_path(1)]
    reopened.database.export()
    assert Path(str(reopened.chapter_md_path(1))).read_text(encoding="utf-8") == prose
    assert reopened.database.integrity()["ok"]


def test_transaction_failure_rolls_back_state_events_prose_and_exports(tmp_path):
    store = _store(tmp_path, chapters=2)
    original = store.read_head()
    export = Path(str(store.head_path)).read_bytes()
    with pytest.raises(RuntimeError):
        with store.transaction():
            history(store, 1)
            raise RuntimeError("crash after writes")
    assert store.read_head() == original
    assert Path(str(store.head_path)).read_bytes() == export
    assert not store.chapter_md_path(1).exists()
    assert store.events_path.read_text(encoding="utf-8") == ""
    with store.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_cross_session_recall_finds_old_joint_event_and_knowledge(tmp_path):
    store = _store(tmp_path, chapters=100)
    history(store, 1)
    history(store, 2, who="丙", target="丁", quote="无关角色去了另一个地方", topic="无关", asset="新信")
    head = store.read_head(); head.update(last_committed_ch=99, last_acked_ch=99); store.write_head(head)
    reopened = BookStore(store.project)
    records = recall(reopened, before_chapter=100, people=["甲", "乙"], topics=["旧约"])["records"]
    assert records and all(row["chapter"] == 1 for row in records)
    assert any(row["kind"] == "knowledge" and row["who"] == "甲" for row in records)
    assert any(row["evidence_status"] == "verified" and row["quote"] == "两人当年约定保守秘密" for row in records)


def test_original_prose_search_finds_unregistered_detail_and_short_chinese_query(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    for query in ("柜底", "旧信藏在柜底"):
        records = recall(store, before_chapter=2, query=query)["records"]
        assert any(row["kind"] == "passage" and query in row["quote"] for row in records)


def test_recall_filters_future_and_preserves_selected_evidence(tmp_path):
    store = _store(tmp_path, chapters=3)
    complete_quote = "两人当年约定保守秘密。" * 120 + "不可丢失的旧约末尾证据"
    history(store, 1, quote=complete_quote, text=complete_quote)
    history(store, 2, quote="第二章才知道的新秘密")
    result = recall(store, before_chapter=2, people=["甲"], max_chars=600, limit=2)
    assert all(row["chapter"] == 1 for row in result["records"])
    size = sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) for row in result["records"])
    assert size == result["chars"] > 600
    assert any(row["quote"] == complete_quote and row["text"] == complete_quote for row in result["records"])
    assert all(row["prose_path"] == str(store.chapter_md_path(1)) for row in result["records"])
    with pytest.raises(LedgerError):
        recall(store, before_chapter=0)


def test_explicit_old_event_is_prioritized_and_missing_reference_is_rejected(tmp_path):
    store = _store(tmp_path, chapters=3)
    history(store, 1)
    with store.database.connection() as conn:
        ident = conn.execute("SELECT id FROM events WHERE chapter=1").fetchone()[0]
    history(store, 2, quote="第二章才知道的新秘密")
    result = recall(store, before_chapter=3, people=["甲"], event_ids=[ident], limit=1)
    assert result["records"][0]["event_id"] == ident
    with pytest.raises(LedgerError) as exc:
        recall(store, before_chapter=3, event_ids=["missing-event"])
    assert exc.value.code == "missing_memory_ref"


def test_patch_invalidates_old_quote_and_keeps_revision_history(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    store.chapter_md_path(1).write_text("两人没有作出任何承诺。\n", encoding="utf-8")
    rows = recall(store, before_chapter=2, topics=["旧约"])["records"]
    assert rows and all(row["evidence_status"] == "record_only" and not row["quote"] for row in rows)
    assert not any(row["kind"] == "passage" for row in recall(store, before_chapter=2, query="柜底")["records"])
    with store.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2


def test_rollback_removes_ghost_memory_and_rewrite_has_new_revision(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    rollback_ledger_to(store, 1)
    store.chapter_md_path(1).unlink()
    assert not recall(store, before_chapter=2, topics=["旧约"])["records"]
    history(store, 1, quote="两人重新相遇时没有立下旧约", topic="新约")
    assert not recall(store, before_chapter=2, topics=["旧约"])["records"]
    with store.database.connection() as conn:
        assert conn.execute("SELECT max(revision) FROM revisions").fetchone()[0] == 2


def test_pack_delivers_recall_to_writer_and_reviewer_only(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1, who="主角", target="乙")
    pack = assemble_pack(store, 2)
    assert pack["historical_recall"]["records"]
    draft = make_draft_view(pack)
    assert "两人当年约定保守秘密" in draft["writing_brief"]
    assert "historical_recall" not in draft
    assert "historical_recall" not in make_polish_view(pack)
    assert make_assemble_view(pack)["historical_recall"] == pack["historical_recall"]


def test_legacy_migration_is_idempotent_and_retains_sources(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    original = store.load_config()
    store.database_path.unlink()
    with pytest.raises(LedgerError) as exc:
        store.load_config()
    assert exc.value.code == "database_migration_required"
    result = migrate(store)
    assert result["documents"] > 5
    assert store.load_config() == original
    assert Path(str(store.config_path)).is_file()
    assert recall(store, before_chapter=2, topics=["旧约"])["records"]
    assert migrate(store)["already_migrated"] is True


def test_failed_migration_leaves_no_database(tmp_path):
    store = _store(tmp_path, chapters=2)
    store.database_path.unlink()
    Path(str(store.plan_path)).write_text('{broken', encoding="utf-8")
    with pytest.raises(LedgerError) as exc:
        migrate(store)
    assert exc.value.code == "migration_invalid_document"
    assert not store.database.exists()


def test_cli_memory_query_is_read_only_and_database_export_is_recoverable(tmp_path, capsys):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    before = store.database_path.read_bytes()
    assert main(["memory", "recall", "--project", str(store.project), "--person", "甲"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["records"]
    assert store.database_path.read_bytes() == before
    assert main(["database", "verify", "--project", str(store.project)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    destination = tmp_path / "export"
    assert main(["database", "export", "--project", str(store.project), "--destination", str(destination)]) == 0
    capsys.readouterr()
    assert (destination / "config.json").is_file()


def test_cli_rejects_legacy_writes_before_creating_database(tmp_path, capsys):
    store = _store(tmp_path, chapters=2)
    store.database_path.unlink()
    head = Path(str(store.head_path)).read_bytes()
    assert main(["chapter", "next", "--project", str(store.project)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "database_migration_required"
    assert not store.database.exists()
    assert Path(str(store.head_path)).read_bytes() == head


def test_multiple_explicit_events_each_receive_a_slot(tmp_path):
    store = _store(tmp_path, chapters=3)
    history(store, 1)
    history(store, 2, quote="第二章回望当年的约定")
    with store.database.connection() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM events ORDER BY seq")]
    result = recall(store, before_chapter=3, event_ids=ids, limit=2, max_chars=4000)
    assert {row["event_id"] for row in result["records"]} == set(ids)


def test_knowledge_history_marks_superseded_belief(tmp_path):
    store = _store(tmp_path, chapters=3)
    history(store, 1, text="人物曾经相信旧说")
    history(store, 2, text="后来亲眼确认了新说", quote="后来亲眼确认了新说")
    rows = [row for row in recall(store, before_chapter=3, topics=["旧约"])["records"] if row["kind"] == "knowledge"]
    assert [(row["chapter"], row["latest_knowledge"]) for row in rows] == [(2, True), (1, False)]
    assert all(row["stance"] == "knows" and row["source"] == "亲见" for row in rows)
    old = recall(store, before_chapter=2, topics=["旧约"])["records"]
    assert old[0]["latest_knowledge"] is True


def test_event_projection_rebuild_retains_unchanged_suffix(tmp_path):
    store = _store(tmp_path, chapters=3)
    history(store, 1)
    history(store, 2, quote="第二章仍然存在的引文")
    events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
    events[0]["l1_summary"] = "第一章摘要修订"
    store.events_path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n")
    with store.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert any(row["chapter"] == 2 for row in recall(store, before_chapter=3, topics=["旧约"])["records"])


def test_multiline_quote_verifies_against_full_prose(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1, quote="第一行承诺。\n第二行继续承诺")
    rows = recall(store, before_chapter=2, topics=["旧约"])["records"]
    assert rows[0]["evidence_status"] == "verified"
    assert "\n" in rows[0]["quote"]


def test_source_import_and_online_backup_preserve_database_authority(tmp_path, capsys):
    store = _store(tmp_path, chapters=2)
    source = tmp_path / "intent.txt"
    source.write_text("作者确认的新意图", encoding="utf-8")
    assert main(["database", "import-source", "--project", str(store.project), "--kind", "intent", "--source", str(source)]) == 0
    capsys.readouterr()
    assert store.intent_path.read_text() == "作者确认的新意图"
    anchor = tmp_path / "anchor.txt"
    anchor.write_bytes("参考叙述片段不提供本书剧情事实。".encode("gb18030"))
    assert main(["database", "import-source", "--project", str(store.project), "--kind", "anchor", "--source", str(anchor)]) == 0
    capsys.readouterr()
    from novel_ledger_core.voice.anchor import build_voice_anchor
    cfg = store.load_config()
    output = build_voice_anchor(store.project, cfg, 1)
    assert "参考叙述" in output
    Path(str(store.memory_dir / "voice-anchor.txt")).unlink()
    assert build_voice_anchor(store.project, cfg, 1) == output
    history(store, 1)
    store.chapter_md_path(1).write_text("修订后的当前正文。", encoding="utf-8")
    backup = tmp_path / "snapshot.sqlite3"
    before = store.database_path.read_bytes()
    assert main(["database", "backup", "--project", str(store.project), "--destination", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert store.database_path.read_bytes() == before
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migration_keeps_accepted_stage_input_authoritative(tmp_path):
    store = _store(tmp_path, chapters=2)
    store.draft_text_path(1).write_text("已经验收但尚未入账的草稿", encoding="utf-8")
    head = store.read_head(); head.update(chapter=1, phase="await_polish"); store.write_head(head)
    store.database_path.unlink()
    migrate(store)
    draft = store.draft_text_path(1)
    Path(str(draft)).write_text("外部文件被改写", encoding="utf-8")
    assert draft.read_text() == "已经验收但尚未入账的草稿"


def test_query_falls_back_when_fts5_is_unavailable(tmp_path):
    store = _store(tmp_path, chapters=2)
    history(store, 1)
    with store.database.connection(write=True) as conn:
        conn.execute("DROP TRIGGER IF EXISTS passage_insert")
        conn.execute("DROP TRIGGER IF EXISTS passage_delete")
        conn.execute("DROP TABLE IF EXISTS passage_fts")
    records = recall(store, before_chapter=2, query="旧信藏在柜底")["records"]
    assert any(row["kind"] == "passage" for row in records)
    store.chapter_md_path(1).write_text("重新修订的普通段落。", encoding="utf-8")
    rows = recall(store, before_chapter=2, query="旧信藏在柜底")["records"]
    assert all(row["kind"] != "passage" and row["evidence_status"] == "record_only" for row in rows)


def test_failed_canon_import_rolls_back_documents_and_exports(tmp_path, capsys):
    store = _store(tmp_path, chapters=2)
    cards = store.kb_path.read_bytes()
    source = tmp_path / "invalid-canon"
    source.mkdir()
    (source / "invalid.json").write_text('{broken', encoding="utf-8")
    assert main(["database", "import-source", "--project", str(store.project), "--kind", "canon", "--source", str(source)]) == 1
    capsys.readouterr()
    assert store.kb_path.read_bytes() == cards
    assert not (store.canon_dir / "invalid.json").exists()
    assert not Path(str(store.canon_dir / "invalid.json")).exists()
