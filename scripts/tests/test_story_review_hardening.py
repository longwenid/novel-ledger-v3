"""Independent review regression tests for receipt/fence and demand-loaded evidence."""
from __future__ import annotations

import copy

import pytest

from novel_ledger_core.content.story_review import (
    pending_review,
    prepare_review, receipts, submit_review,
)
from novel_ledger_core.content.extract import canon_source_fingerprint, extract_cards
from novel_ledger_core.control.autopilot import run_one_job
from novel_ledger_core.control.pipeline import book_complete, book_reopen, chapter_next
from novel_ledger_core.infra.util import LedgerError, atomic_json, atomic_text, read_json
from tests.test_story_review import pass_pending, review_output, story_store, write_chapter


def _prepared(tmp_path, *, completing=False):
    store = story_store(tmp_path)
    write_chapter(store)
    write_chapter(store)
    prepare_review(store, pending_review(store, completing=completing))
    return store


@pytest.mark.parametrize("status", ["BLOCKER", "UNVERIFIABLE"])
def test_same_revision_cannot_overwrite_failing_receipt_with_pass(tmp_path, status):
    store = _prepared(tmp_path)
    failed = review_output(store)
    failed["checks"][0].update(status=status, reason="尚未兑现签约的角色选择与代价", evidence=[])
    assert submit_review(store, failed)["verdict"] == "fix"
    previous = copy.deepcopy(receipts(store))
    with pytest.raises(LedgerError) as raised:
        submit_review(store, review_output(store))
    assert raised.value.code == "story_review_blocked"
    assert receipts(store) == previous
    assert chapter_next(store)["action"] == "story_review_blocked"


def test_identical_receipt_retry_is_idempotent_and_can_finish_head_write(tmp_path, monkeypatch):
    store = _prepared(tmp_path)
    output = review_output(store)
    write_head = store.write_head
    def crash(*args, **kwargs):
        raise RuntimeError("crash after review receipt")
    monkeypatch.setattr(store, "write_head", crash)
    with pytest.raises(RuntimeError):
        submit_review(store, output)
    previous = copy.deepcopy(receipts(store))
    assert store.read_head().get("last_story_review") is None
    monkeypatch.setattr(store, "write_head", write_head)
    assert submit_review(store, output)["verdict"] == "pass"
    assert receipts(store) == previous
    assert store.read_head()["last_story_review"]["review_id"] == output["review_id"]


@pytest.mark.parametrize("field", ["review_id", "review_input_hash"])
def test_review_cannot_submit_under_another_active_job_binding(tmp_path, field):
    store = _prepared(tmp_path)
    output = review_output(store)
    active = {"initial_action": "story_review", "review_id": output["review_id"], "review_input_hash": output["input_hash"]}
    active[field] = "a different isolated review"
    atomic_json(store.autopilot_active_job_path, active)
    with pytest.raises(LedgerError) as raised:
        submit_review(store, output)
    assert raised.value.code == "story_review_job_mismatch"
    assert receipts(store) == {}


def test_book_promises_are_loaded_only_for_book_scope_without_clipping(tmp_path):
    store = _prepared(tmp_path)
    plan = store.load_plan()
    plan["book_outline"] = {"long_term_commitments": [{"id": "long", "promise": "全书终局必须兑现" * 40_000 + "末尾终局承诺"}]}
    store.save_plan(plan)
    # A completed volume does not receive the entire later endgame promise list.
    prepare_review(store, pending_review(store, completing=True))
    assert read_json(store.staging_dir / "story-review-view.json")["book_promises"] == []
    submit_review(store, review_output(store))
    # The book reviewer needs the complete promise, regardless of its length.
    spec = pending_review(store, completing=True)
    assert spec["scope"] == "book"
    prepare_review(store, spec)
    view = read_json(store.staging_dir / "story-review-view.json")
    assert view["book_promises"] == plan["book_outline"]["long_term_commitments"]



def test_long_review_reason_is_stored_in_full(tmp_path):
    store = _prepared(tmp_path)
    output = review_output(store)
    reason = "有据的审稿意见。" * 40_000 + "末尾审稿结论"
    output["checks"][0]["reason"] = reason
    assert submit_review(store, output)["verdict"] == "pass"
    assert receipts(store)[output["review_id"]]["checks"][0]["reason"] == reason



def test_matching_hash_with_missing_persisted_checks_is_not_a_passing_receipt(tmp_path):
    store = _prepared(tmp_path)
    output = review_output(store)
    submit_review(store, output)
    stored = receipts(store)
    stored[output["review_id"]]["checks"] = []
    atomic_json(store.editorial_dir / "story-reviews.json", stored)
    with pytest.raises(LedgerError) as raised:
        pending_review(store)
    assert raised.value.code == "invalid_story_reviews"


def test_malformed_persisted_receipt_is_a_domain_error(tmp_path):
    store = _prepared(tmp_path)
    atomic_json(store.editorial_dir / "story-reviews.json", {"volume:vol-0001:1-2": "corrupted receipt"})
    with pytest.raises(LedgerError) as raised:
        pending_review(store)
    assert raised.value.code == "invalid_story_reviews"


def test_empty_legacy_canon_without_a_source_baseline_can_be_reviewed(tmp_path):
    store = _prepared(tmp_path)
    atomic_json(store.kb_path, {"cards": store.load_kb()})
    assert pending_review(store)["scope"] == "volume"


@pytest.mark.parametrize("change", ["added", "changed", "removed"])
def test_review_requires_synced_current_original_canon(tmp_path, change):
    store = _prepared(tmp_path)
    path = store.canon_dir / "world.md"
    if change != "added":
        atomic_text(path, "## 世界规则\n遗书必须由持有人当面交出。\n")
        store.save_kb(extract_cards(store.canon_dir)["cards"], source_fingerprint=canon_source_fingerprint(store.canon_dir))
        prepare_review(store, pending_review(store))
    output = review_output(store)
    if change == "removed":
        path.unlink()
    else:
        atomic_text(path, "## 世界规则\n遗书必须由持有人付出代价后交出。\n")
    for review in (lambda: pending_review(store), lambda: submit_review(store, output)):
        with pytest.raises(LedgerError) as raised:
            review()
        assert raised.value.code == "story_review_canon_unsynced"
    assert receipts(store) == {}
    store.save_kb(extract_cards(store.canon_dir)["cards"], source_fingerprint=canon_source_fingerprint(store.canon_dir))
    prepare_review(store, pending_review(store))
    assert submit_review(store, review_output(store))["verdict"] == "pass"


def test_normal_completed_book_rechecks_changed_omitted_source_before_run_success(tmp_path):
    store = story_store(tmp_path, chapters=2)
    source = "## 主线与终局\n保留的主线依据。\n## 历史场景\n" + "省略场景正文。" * 700 + "省略尾部旧标记。"
    atomic_text(store.outline_path, source)
    write_chapter(store)
    write_chapter(store)
    pass_pending(store, completing=True)
    pass_pending(store, completing=True)
    book_complete(store, actor="作者", reason="全部复核通过")
    assert store.read_head()["completion_kind"] == "normal"
    atomic_text(store.outline_path, source.replace("尾部旧标记", "尾部新标记"))
    assert pending_review(store, completing=True)["scope"] == "volume"
    with pytest.raises(LedgerError) as raised:
        run_one_job(store, {}, lease=None, state={})
    assert raised.value.code == "story_review_required"
    assert book_reopen(store, actor="作者", reason="来源修订后重新复核")["phase"] == "idle"
    assert chapter_next(store)["action"] == "story_review"
