from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from .pipeline import (
    ack_read,
    authorize_usage_resume,
    audit_book,
    book_complete,
    book_close_early,
    book_reopen,
    chapter_next,
    check_submit_output,
    precheck_prose,
    retry_authorize,
    record_usage,
    status,
    submit_output,
)
from ..infra.store import BookStore
from ..infra.util import LedgerError, ok, read_json, stable_read_json


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LedgerError("invalid_args", message)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0 if payload.get("ok") else 1


def _project(args: argparse.Namespace) -> BookStore:
    if not getattr(args, "project", None):
        raise LedgerError("missing_project", "--project is required")
    return BookStore(Path(args.project))


def _dotted_config_get(cfg: dict[str, Any], dotted: str | None) -> Any:
    node: Any = cfg
    for part in [p for p in str(dotted or "").split(".") if p.strip()]:
        if not isinstance(node, dict) or part not in node:
            raise LedgerError(
                "unknown_config_key",
                f"config has no key {dotted!r}",
                {"key": dotted, "known_top_level": sorted(k for k in cfg)},
            )
        node = node[part]
    return node


def _dotted_config_set(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    """只能改已有键：新增键一律点名拒收，把 `polsh` 这类手滑挡在写入前。"""
    parts = [p for p in str(dotted or "").split(".") if p.strip()]
    if not parts:
        raise LedgerError("unknown_config_key", "config set needs a non-empty --key")
    node: dict[str, Any] = cfg
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            raise LedgerError(
                "unknown_config_key",
                f"config set cannot descend into {part!r}",
                {"key": dotted, "known_top_level": sorted(k for k in cfg)},
            )
        node = node[part]
    leaf = parts[-1]
    if leaf not in node:
        known = sorted(node) if node is not cfg else sorted(k for k in cfg)
        raise LedgerError(
            "unknown_config_key",
            f"config set can only change existing keys; {leaf!r} is not one (typo?). Known keys: {known}",
            {"key": dotted},
        )
    node[leaf] = value


def _parse_config_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _usage_from_cli(args: argparse.Namespace) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in (
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "total_tokens",
    ):
        val = getattr(args, key, None)
        if val is not None:
            usage[key] = int(val)
    return usage


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonParser(prog="novel_ledger", description="novel-ledger control plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--project", required=False, help="novel project directory")
    parser.add_argument(
        "--terse",
        action="store_true",
        help="strip orchestration envelopes (worker brief / execution contract) from output; "
        "for worker sessions that re-send every byte of stdout as context",
    )
    def add_project(p: argparse.ArgumentParser) -> None:
        # default=SUPPRESS 是关键（否则 `--project X status` 会报 missing_project）：
        # subparser 会把自身默认值整体覆写父 namespace（argparse 3.7+ 行为），普通 None 默认
        # 会把前置 --project=X 冲回 None；SUPPRESS 时该属性缺省不参与覆写，先置后置皆生效。
        # 与 _register_terse_on_subparsers（--terse 的同款修复，7341584）保持同一模式。
        p.add_argument("--project", required=False, default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_database = sub.add_parser("database", help="SQLite migration, verification and exports")
    database_sub = p_database.add_subparsers(dest="database_cmd", required=True)
    for name in ("migrate", "verify", "export", "backup"):
        command = database_sub.add_parser(name)
        add_project(command)
        if name == "backup":
            command.add_argument("--destination", required=True)
        if name == "export":
            command.add_argument("--destination", help="optional directory for derived project files")
    command = database_sub.add_parser("import-source")
    add_project(command)
    command.add_argument("--source", required=True)
    command.add_argument("--kind", choices=["canon", "intent", "outline", "anchor"], required=True)
    p_memory = sub.add_parser("memory", help="targeted historical evidence retrieval")
    memory_sub = p_memory.add_subparsers(dest="memory_cmd", required=True)
    command = memory_sub.add_parser("recall")
    add_project(command)
    command.add_argument("--before-chapter", type=int)
    command.add_argument("--person", action="append", default=[])
    command.add_argument("--topic", action="append", default=[])
    command.add_argument("--asset", action="append", default=[])
    command.add_argument("--event-id", action="append", default=[])
    command.add_argument("--query", default="")
    command.add_argument("--limit", type=int, default=12)
    command.add_argument("--max-chars", type=int, default=0, help="deprecated compatibility option; selected records remain complete")
    p_review = sub.add_parser("review", help="durable editorial findings and patch review")
    review_sub = p_review.add_subparsers(dest="review_cmd", required=True)
    p_review_list = review_sub.add_parser("list")
    add_project(p_review_list)
    p_review_list.add_argument("--chapter", type=int)
    p_review_list.add_argument("--limit", type=int, default=50)
    p_review_resolve = review_sub.add_parser("resolve")
    add_project(p_review_resolve)
    p_review_resolve.add_argument("--id", required=True)
    p_review_resolve.add_argument("--disposition", choices=["accepted", "deferred", "closed"], required=True)
    p_review_resolve.add_argument("--actor", required=True)
    p_review_resolve.add_argument("--reason", required=True)
    p_patch_ack = review_sub.add_parser("ack-patch")
    add_project(p_patch_ack)
    p_patch_ack.add_argument("--chapter", type=int, required=True)
    p_patch_ack.add_argument("--quote", action="append", required=True)
    p_story_next = review_sub.add_parser("story-next", help="prepare the mandatory pending volume/endgame review")
    add_project(p_story_next)
    p_story_next.add_argument("--complete", action="store_true", help="include the final volume and book review")
    p_story_submit = review_sub.add_parser("story-submit", help="submit an evidence-bearing narrative review")
    add_project(p_story_submit)
    p_story_submit.add_argument("--output", required=True)
    p_story_evidence = review_sub.add_parser("story-evidence", help="export one complete requested chapter in the prepared review")
    add_project(p_story_evidence)
    p_story_evidence.add_argument("--chapter", required=True, type=int)

    p_status = sub.add_parser("status")
    add_project(p_status)
    p_status.add_argument(
        "--card",
        action="store_true",
        help="dispatcher card: phase/chapter/blocked/progress only (no diagnostics volume)",
    )

    p_config = sub.add_parser(
        "config",
        help="read/write book config (the database is authoritative; config.json is a projected view)",
    )
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)
    p_config_get = config_sub.add_parser("get", help="read config (whole or one dotted key)")
    add_project(p_config_get)
    p_config_get.add_argument("--key", default=None, help="dotted key path, e.g. polish or word_band.min")
    p_config_set = config_sub.add_parser(
        "set",
        help="set one existing config key through the authoritative channel",
    )
    add_project(p_config_set)
    p_config_set.add_argument("--key", required=True, help="dotted key path, e.g. polish")
    p_config_set.add_argument(
        "--value",
        required=True,
        help="JSON value; unparseable values stay strings (on/off stay strings)",
    )
    p_config_set.add_argument("--actor", default="unspecified", help="who or what is changing the config")

    p_run = sub.add_parser("run", help="unattended session-host helpers (checkpoint / handoff / resume)")
    run_sub = p_run.add_subparsers(dest="run_cmd", required=True)
    p_run_status = run_sub.add_parser("status", help="read unattended run state (checkpoint cursor)")
    add_project(p_run_status)
    p_run_handoff = run_sub.add_parser(
        "handoff",
        help="write book/run/handoff.md: HEAD, next step, due hooks, open findings (read-only)",
    )
    add_project(p_run_handoff)
    p_run_checkpoint = run_sub.add_parser(
        "checkpoint",
        help="run the due batch/volume machine checkpoint now (zero model calls)",
    )
    add_project(p_run_checkpoint)
    p_run_resume = run_sub.add_parser(
        "resume",
        help="clear a paused run state (e.g. review_required) and reset an unhonored plan nudge",
    )
    add_project(p_run_resume)

    p_ledger = sub.add_parser("ledger", help="ledger self-checks (event replay vs snapshot)")
    ledger_sub = p_ledger.add_subparsers(dest="ledger_cmd", required=True)
    p_lverify = ledger_sub.add_parser(
        "verify",
        help="replay events.jsonl and compare against snapshot.json (read-only)",
    )
    add_project(p_lverify)
    p_lrepair = ledger_sub.add_parser(
        "repair",
        help="rebuild snapshot.json from events.jsonl (events are the source of truth)",
    )
    add_project(p_lrepair)
    p_lrepair.add_argument(
        "--from-events",
        action="store_true",
        required=True,
        help="rebuild the snapshot by replaying events.jsonl (the only supported repair)",
    )

    p_ch = sub.add_parser("chapter")
    ch_sub = p_ch.add_subparsers(dest="chapter_cmd", required=True)
    p_next = ch_sub.add_parser("next")
    add_project(p_next)
    p_next.add_argument(
        "--card",
        action="store_true",
        help="dispatcher card: action/phase/worker pointers only; full envelope stays on disk "
        "(stage-agent host sessions; executor sessions get the full payload)",
    )
    p_precheck = ch_sub.add_parser(
        "precheck",
        help="writer self-check on a staging prose file: word band + style hard red lines (read-only, costs no quota)",
    )
    add_project(p_precheck)
    p_precheck.add_argument("--file", required=True, help="draft/polished prose text file to check")
    p_precheck.add_argument("--chapter", type=int, default=None, help="chapter number for the report")
    p_submit = ch_sub.add_parser("submit")
    add_project(p_submit)
    p_submit.add_argument("--output", required=True, help="writer output JSON")
    p_submit.add_argument(
        "--force",
        action="store_true",
        help="deprecated compatibility option; submit always runs all gates",
    )

    p_usage = ch_sub.add_parser(
        "usage-record",
        help="record one incremental host usage sample and evaluate the chapter budget",
    )
    add_project(p_usage)
    p_usage.add_argument("--stage", required=True, help="draft/polish/assemble/ack or host-defined stage label")
    p_usage.add_argument(
        "--chapter",
        type=int,
        required=True,
        help="required: echo the chapter from the model action being accounted",
    )
    p_usage.add_argument("--request-id", required=True, help="stable idempotency key for this model request")
    p_usage.add_argument("--session-id", default="", help="optional host model-session id for diagnostics")
    p_usage.add_argument(
        "--uncached-input-tokens",
        type=int,
        default=None,
        help="component shape: uncached input tokens (omit when only --total-tokens is known)",
    )
    p_usage.add_argument("--cache-read-input-tokens", type=int, default=None)
    p_usage.add_argument("--cache-write-input-tokens", type=int, default=None)
    p_usage.add_argument("--output-tokens", type=int, default=None)
    p_usage.add_argument(
        "--total-tokens",
        type=int,
        default=None,
        help="total-only shape for hosts that only see a whole-job total (e.g. stage-agent "
        "subagent totals); mutually exclusive with the component counters",
    )

    p_usage_authorize = ch_sub.add_parser(
        "usage-authorize",
        help="authorize only bounded final review after the current chapter token stop",
    )
    add_project(p_usage_authorize)
    p_usage_authorize.add_argument("--chapter", type=int, default=None)
    p_usage_authorize.add_argument("--action", choices=["ack"], default="ack")
    p_usage_authorize.add_argument("--actor", required=True)
    p_usage_authorize.add_argument("--reason", required=True)

    p_usage_void = ch_sub.add_parser(
        "usage-void",
        help="void a mis-recorded usage sample by request-id (zero-model correction; append-only)",
    )
    add_project(p_usage_void)
    p_usage_void.add_argument("--request-id", required=True, help="request-id of the sample to void")
    p_usage_void.add_argument("--reason", required=True, help="why this sample is being voided")

    p_check_submit = ch_sub.add_parser(
        "check-submit",
        aliases=["validate-submit"],
        help="run the submit gates read-only without consuming rewrite quota",
    )
    add_project(p_check_submit)
    p_check_submit.add_argument("--output", required=True, help="writer output JSON to inspect")

    # 这两个提交只认 staging 里的固定落点，不需要参数；但仍然接受一个可选的 --file。
    # 理由：任务书把「先用 `chapter precheck --file` 自查，再 draft-submit」
    # 写在同一个句子里，worker 常照着写成 `draft-submit --file <path>`，被 argparse
    # 判成 invalid_args 白损失一轮。这里让直觉写法可用——只在它指向的就是该阶段落点时才放行，
    # 写错路径要明确报错，避免"随便指一个文件就当提交了"。
    _submit_file_help = (
        "optional: must be the stage's own staging file (draft/polish output path); "
        "the submit itself always reads that fixed staging path"
    )
    p_draft = ch_sub.add_parser("draft-submit", help=" accept the content draft text into the ledger")
    add_project(p_draft)
    p_draft.add_argument("--file", default=None, help=_submit_file_help)
    p_polish = ch_sub.add_parser("polish-submit", help=" accept the polished text into the ledger")
    add_project(p_polish)
    p_polish.add_argument("--file", default=None, help=_submit_file_help)

    p_ack = ch_sub.add_parser("ack-read")
    add_project(p_ack)
    # ack 只服务当前活动章，本无章节参数；实战里 worker 按直觉带上 --chapter 被
    # argparse 判 unrecognized 直接退出（同 draft-submit --file 的老坑）。这里放行
    # 直觉写法：带了就必须对得上活动章，对不上明确报错，不给"静默改判他章"留门。
    p_ack.add_argument(
        "--chapter",
        type=int,
        default=None,
        help="optional: intuitive echo of the chapter under review; must match the active ack chapter",
    )
    p_ack.add_argument("--quote", action="append", default=[], help="sentence copied from committed prose")
    p_ack.add_argument("--quotes-file", help="JSON list of quotes")
    p_ack.add_argument("--verdict", default="pass", choices=["pass", "p0"])
    p_ack.add_argument("--voice-note", default="")
    p_ack.add_argument("--prose-hash", default="")
    p_ack.add_argument("--findings-file", help="JSON list of final review findings, including UNVERIFIABLE")

    p_patch = ch_sub.add_parser("patch", help="段落级快速就地修润与自愈沉淀")
    add_project(p_patch)
    p_patch.add_argument("--chapter", type=int, default=None, help="目标章节号（缺省为当前活动章节）")
    p_patch.add_argument("--target", default="", help="要替换的原始段落/句子（必须在正文中唯一存在）")
    p_patch.add_argument("--replacement", default="", help="替换后的新段落/句子")
    p_patch.add_argument("--patch-file", help="包含 target 与 replacement 的 JSON 文件路径")
    p_patch.add_argument("--bypass-style-check", action="store_true", help="跳过文风硬红线机检")

    p_rework = ch_sub.add_parser(
        "rework-patch",
        help="返工定点修：plot_fix/rewrite 轮转稿就地锚定修复，不开整章返工 worker",
    )
    add_project(p_rework)
    p_rework.add_argument("--target", default="", help="要替换的原始句子（必须在轮转稿中唯一存在）")
    p_rework.add_argument("--replacement", default="", help="替换后的新句子")
    p_rework.add_argument("--patch-file", help="包含 target 与 replacement 的 JSON 对象或列表路径")
    p_rework.add_argument("--bypass-style-check", action="store_true", help="跳过文风硬红线机检")

    p_retry = sub.add_parser("retry-authorize")
    add_project(p_retry)
    p_retry.add_argument("--actor", required=True)
    p_retry.add_argument("--reason", required=True)
    p_retry.add_argument(
        "--action",
        choices=["rewrite", "style"],
        default="rewrite",
        help=(
            "rewrite = default full-chapter rollback; style = light unlock for "
            "blocked(style_metrics_failed): restore retry budget in place, no rollback"
        ),
    )

    p_relations = sub.add_parser("relations", help="relation ledger governance (rename, close)")
    rel_sub = p_relations.add_subparsers(dest="relations_cmd", required=True)
    p_rel_rename = rel_sub.add_parser(
        "rename", help="unify a relation name variant with an append-only ruling"
    )
    add_project(p_rel_rename)
    p_rel_rename.add_argument("--from", dest="from_name", required=True, help="variant name to merge away")
    p_rel_rename.add_argument("--to", dest="to_name", required=True, help="canonical surviving name")
    p_rel_rename.add_argument("--actor", default="unspecified", help="editor making this ruling")
    p_rel_rename.add_argument("--reason", default="", help="reason for the canonical name")
    p_rel_close = rel_sub.add_parser(
        "close", help="converge same-pair relations (relation_pair_conflict) with a ruling"
    )
    add_project(p_rel_close)
    p_rel_close.add_argument("--who", required=True)
    p_rel_close.add_argument("--target", required=True)
    p_rel_close.add_argument("--kind", default="", help="substring of kind to match (empty = all entries of the pair)")
    p_rel_close.add_argument("--reason", required=True, help="ruling recorded into close_reason")
    p_rel_close.add_argument("--actor", default="unspecified", help="editor making this ruling")

    p_book = sub.add_parser("book")
    book_sub = p_book.add_subparsers(dest="book_cmd", required=True)
    p_done = book_sub.add_parser("complete")
    add_project(p_done)
    p_done.add_argument("--actor", required=True)
    p_done.add_argument("--reason", required=True)
    p_done.add_argument(
        "--override-target",
        action="store_true",
        help="author explicitly shortens the signed book target; required below 90% word progress",
    )
    p_early = book_sub.add_parser("close-early", help="record author-approved early closure, separately from normal completion")
    add_project(p_early)
    p_early.add_argument("--actor", required=True)
    p_early.add_argument("--reason", required=True)
    p_early.add_argument("--author-confirmed", action="store_true", required=True)
    p_reopen = book_sub.add_parser("reopen", help="undo `book complete`: reopen the write path")
    add_project(p_reopen)
    p_reopen.add_argument("--actor", required=True)
    p_reopen.add_argument("--reason", required=True)
    p_audit = book_sub.add_parser("audit", help="audit whole book consistency, word stats, lore, and quotes")
    add_project(p_audit)
    p_reconcile = book_sub.add_parser(
        "reconcile",
        help="cross-chapter numeric reconciliation: enumerate quote/gate drift into one triage list",
    )
    add_project(p_reconcile)
    p_calibrate = book_sub.add_parser(
        "calibrate",
        help="propose quant_keys candidates from the 量化口径 canon card (read-only)",
    )
    add_project(p_calibrate)
    p_facts = book_sub.add_parser(
        "facts",
        help="cross-chapter fact registry check: declared value ranges, format, near-duplicates (read-only)",
    )
    add_project(p_facts)
    p_resync = book_sub.add_parser("resync-baseline", help="recompute sha256 for all chapters, resync meta/acks and heal quotes")
    add_project(p_resync)
    p_resync.add_argument("--no-fix-quotes", action="store_true", help="do not auto-heal invalid quotes")
    p_resync.add_argument(
        "--restamp-canon",
        action="store_true",
        help="also re-stamp the current canon fingerprint (clears canon_drift; only after a reviewed, no-rewrite canon edit)",
    )
    p_hatch = book_sub.add_parser(
        "hatch",
        help="choice-driven book hatching: validate choices manifest and initialize project",
    )
    add_project(p_hatch)
    p_hatch.add_argument(
        "--manifest",
        required=False,
        help="choice-driven hatch manifest JSON (required unless --sample-manifest)",
    )
    p_hatch.add_argument(
        "--check-only",
        action="store_true",
        help="validate manifest and voice without creating or changing a project",
    )
    p_hatch.add_argument(
        "--sample-manifest",
        action="store_true",
        help="emit the same-source valid sample manifest (no project needed); "
        "edit values, keep the shape, then iterate with --check-only",
    )

    p_hooks = sub.add_parser("hooks", help="hooks lifecycle management (audit, close, defer)")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_cmd", required=True)
    p_haudit = hooks_sub.add_parser("audit", help="audit active, overdue and closed hooks")
    add_project(p_haudit)
    p_hclose = hooks_sub.add_parser("close", help="close an obsolete or resolved hook")
    add_project(p_hclose)
    p_hclose.add_argument("--id", required=True, help="hook id")
    p_hclose.add_argument("--reason", required=True, help="reason for closing")
    p_hclose.add_argument("--actor", default="unspecified", help="editor making this ruling")
    p_hdefer = hooks_sub.add_parser("defer", help="defer/extend hook due chapter")
    add_project(p_hdefer)
    p_hdefer.add_argument("--id", required=True, help="hook id")
    p_hdefer.add_argument("--due", type=int, required=True, help="new due chapter")
    p_hdefer.add_argument("--actor", default="unspecified", help="editor making this ruling")
    p_hdefer.add_argument("--reason", default="", help="reason for rescheduling")
    p_hdefer.add_argument(
        "--force",
        action="store_true",
        help="waive the signed-plan pin guard: defer even while unwritten beats still pin the old "
        "due (recorded as forced_over_pinned in the governance event)",
    )
    p_hmerge = hooks_sub.add_parser(
        "merge",
        help="merge duplicate hooks by an append-only ruling",
    )
    add_project(p_hmerge)
    p_hmerge.add_argument("--from", dest="from_id", required=True, help="hook id to be merged away")
    p_hmerge.add_argument("--into", dest="into_id", required=True, help="surviving hook id")
    p_hmerge.add_argument("--actor", default="unspecified", help="editor making this ruling")
    p_hmerge.add_argument("--reason", default="", help="reason for merging")

    p_kb = sub.add_parser("kb")
    kb_sub = p_kb.add_subparsers(dest="kb_cmd", required=True)
    p_extract = kb_sub.add_parser("extract")
    p_extract.add_argument("--source", required=True, help="directory of .md (yaml/json allowed)")
    p_extract.add_argument("--out", required=True, help="write {cards:[...]} JSON")
    p_extract.add_argument("--excerpt-max", type=int, default=0, help="deprecated compatibility option; cards retain complete source text")
    p_sync = kb_sub.add_parser("sync", help="extract book/kb/canon → book/kb/cards.json")
    add_project(p_sync)
    p_sync.add_argument("--excerpt-max", type=int, default=0, help="deprecated compatibility option; cards retain complete source text")
    p_plan = sub.add_parser("plan", help="manage the beat source of truth (book/plan/chapters.json)")
    plan_sub = p_plan.add_subparsers(dest="plan_cmd", required=True)
    p_extend = plan_sub.add_parser(
        "extend",
        help="append new chapter beats (numbers must continue after the current max; the default is 5 scene-beats per chapter — fewer is rejected, more is a warning)",
    )
    add_project(p_extend)
    p_extend.add_argument(
        "--chapters",
        required=True,
        help="JSON file: event-spine books require {phase:{...},chapters:[...]}; an optional volumes:{...} refines existing signed volumes (new books preserve their whole-book volume outline; may be used alone without chapters)",
    )

    p_validate = plan_sub.add_parser(
        "validate",
        help="validate chapter beats quality: count, must words, location/present completeness",
    )
    add_project(p_validate)

    p_select_batch = plan_sub.add_parser(
        "select-batch",
        help="validate a plan-batch candidates file (batch brainstorm), write machine fact-checks, and seal the selection as a governance event",
    )
    add_project(p_select_batch)
    p_select_batch.add_argument(
        "--file",
        required=True,
        help="candidates JSON (schema novel-ledger.plan-candidates.v1), e.g. book/editorial/plan-batch-candidates-21-40.json",
    )
    p_select_batch.add_argument(
        "--actor",
        default="unspecified",
        help="who made the selection (recorded truthfully in the governance event)",
    )
    p_select_batch.add_argument(
        "--reason",
        default="",
        help="why the winning candidate won; defaults to a batch-range summary",
    )
    p_rebudget = plan_sub.add_parser("rebudget", help="recalculate remaining chapter capacity from actual word progress, preserving the author word target")
    add_project(p_rebudget)
    p_rebudget.add_argument("--actor", required=True)
    p_rebudget.add_argument("--reason", required=True)
    p_patch_chapter = plan_sub.add_parser(
        "patch-chapter",
        help="surgical in-place fix of an UNWRITTEN chapter's beats/scene fields (plan-level defect channel; governance-event audited)",
    )
    add_project(p_patch_chapter)
    p_patch_chapter.add_argument("--chapter", type=int, required=True, help="chapter number to patch; must be uncommitted")
    p_patch_chapter.add_argument(
        "--patch-file",
        required=True,
        help="JSON object limited to beats/location/present; beats replace the chapter's beats wholesale",
    )
    p_patch_chapter.add_argument("--actor", required=True)
    p_patch_chapter.add_argument("--reason", required=True)
    p_amend = plan_sub.add_parser("amend-contract", help="explicitly re-sign the complete author narrative contract against current editorial sources")
    add_project(p_amend)
    p_amend.add_argument("--file", required=True, help="complete novel-ledger.narrative-contract.v1 JSON object")
    p_amend.add_argument("--actor", required=True)
    p_amend.add_argument("--reason", required=True)

    p_volume_outline = plan_sub.add_parser("volume-outline", help="read the whole-book volume index and one complete selected outline")
    add_project(p_volume_outline)
    p_volume_outline.add_argument("--volume", type=int, help="volume number; omit to read the index")

    p_set_spine = plan_sub.add_parser(
        "set-spine",
        help="write the always-injected world spine (plan.world_spine) through the supported channel",
    )
    add_project(p_set_spine)
    p_set_spine.add_argument("--file", help="UTF-8 text file (BOM tolerated) holding the spine paragraph")
    p_set_spine.add_argument("--text", help="inline spine text")
    p_set_spine.add_argument("--actor", default="unspecified", help="editor making this contract change")
    p_set_spine.add_argument("--reason", required=True, help="why the spine is being written/amended")
    p_context = sub.add_parser("context", help="load a complete source or section relevant to the current task")
    context_sub = p_context.add_subparsers(dest="context_cmd", required=True)
    p_context_read = context_sub.add_parser("read")
    add_project(p_context_read)
    p_context_read.add_argument("--source", required=True, choices=("intent", "outline", "canon"))
    p_context_read.add_argument("--name", help="relative filename within book/kb/canon, required for canon")
    p_context_read.add_argument("--section", help="exact unique heading; selected section includes its subsections in full")

    p_voice = sub.add_parser("voice")
    voice_sub = p_voice.add_subparsers(dest="voice_cmd", required=True)
    p_vapply = voice_sub.add_parser(
        "apply",
        help="refresh or switch project voice memory from references/voices/<id>.md",
    )
    add_project(p_vapply)
    p_vapply.add_argument(
        "--voice",
        default="",
        help="switch to this voice id; omit to refresh the project's current voice",
    )
    p_vapply.add_argument(
        "--cap",
        type=int,
        default=None,
        help="deprecated compatibility option; the chosen voice manual is retained in full",
    )
    voice_sub.add_parser("list", help="list bundled voice manuals under references/voices/")

    # --terse 尾置同样接受（worker 简报教"带 --terse"，LLM 习惯把 flag
    # 追加在命令尾；顶层注册导致尾置 unrecognized → invalid_args，整条 && 链断裂）。
    # 子命令侧补注册，dest 同名——argparse 写同一 namespace，先置后置皆生效。
    _register_terse_on_subparsers(parser)
    return parser


def _register_terse_on_subparsers(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                if not any(act.dest == "terse" for act in sub._actions):
                    # default=SUPPRESS 是关键：subparser 会把自己的默认值整体覆写到父
                    # namespace（argparse 3.7+ 行为），非 SUPPRESS 会把前置 --terse=True
                    # 冲回 False；SUPPRESS 时该属性缺省不参与覆写，先置后置皆生效。
                    sub.add_argument(
                        "--terse",
                        dest="terse",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help="strip orchestration envelopes (same as the global --terse)",
                    )
                _register_terse_on_subparsers(sub)


def _force_utf8_stdio() -> None:
    """输出面统一 UTF-8（幂等）。

    Windows 控制台默认 GBK：CLI JSON 里的中文路径/引文/正文摘句会被换码页打成
    乱码，宿主拿到的路径不可读也不可复用（实测 DSH 会话每条命令都要手工前挂
    PYTHONIOENCODING，还只是部分有效）。进程内能保证的是自己的 stdout/stderr；
    中文**实参**坏码发生在 shell 传参层（pwsh 按系统码页收参），进程内救不了，
    规约见 runtime-platform「文件中转」。pytest/capsys 等替换流没有 reconfigure，
    静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        payload = _dispatch(args)
    except LedgerError as exc:
        return _print(exc.as_dict())
    except Exception as exc:  # noqa: BLE001 — control plane must always emit JSON
        return _print(LedgerError("internal", str(exc)).as_dict())
    if getattr(args, "terse", False):
        payload = _terse_payload(payload)
    return _print(payload)


def _terse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """--terse：剥掉编排信封（worker 简报 / execution 契约）。

    worker 子代理会话里，每次 CLI 调用的 stdout 都留在上下文里被后续请求反复重发；
    worker 简报 ~1.2K 字符 × 每个阶段动作 × 全章请求，是纯重复。这些字段是给
    总编辑（派发方）看的，worker 自己从 spawn prompt 里已经拿到了。
    """
    for key in ("worker", "execution", "context_isolation_required"):
        payload.pop(key, None)
    return payload


def _prevalidate_hatch(args: argparse.Namespace) -> dict[str, Any]:
    """Validate a hatch request before lock acquisition can create book/run/."""
    from ..infra.store import DEFAULT_CONFIG
    from ..voice.voice_manual import build_voice_profile
    from .hatch import validate_hatch_manifest

    manifest = read_json(Path(args.manifest))
    validate_hatch_manifest(manifest)
    voice_id = manifest["voice"]
    build_voice_profile(
        voice_id=str(voice_id),
        pack_cap=int(DEFAULT_CONFIG["pack_caps"]["voice_concepts"]),
    )
    return manifest


def _mutates_story_state(args: argparse.Namespace) -> bool:
    """完结后仍会改故事状态、因而必须按「写路已关」拒绝的命令。

    逐个命令补 `_require_active` 是本仓的旧病——补着补着就漏掉了 `kb sync` / `voice apply` /
    `plan extend` / `hooks close`，它们改的正是 pack 的指纹来源与账本，封笔后无法再通过
    任何章节被记录或消解。这里收口成一张表，新增写命令只在这张表里登记。
    出口是 `book reopen`（显式、可逆、留痕），不是手改 HEAD。

    不在表内、因而**允许**在完结后运行的：`book complete/reopen`（本身就是状态命令）、
    `book resync-baseline` / `ledger repair`（外部手改后的修复路径，
    封掉会把用户困死）、`book audit` 等只读巡检。
    """
    cmd = args.cmd
    if cmd == "kb":
        return args.kb_cmd == "sync"
    if cmd == "voice":
        return getattr(args, "voice_cmd", None) == "apply"
    if cmd == "plan":
        return getattr(args, "plan_cmd", None) in ("extend", "select-batch", "rebudget", "amend-contract", "patch-chapter")
    if cmd == "hooks":
        return getattr(args, "hooks_cmd", None) in ("close", "defer", "merge")
    if cmd == "relations":
        return getattr(args, "relations_cmd", None) in ("rename", "close")
    if cmd == "chapter":
        return getattr(args, "chapter_cmd", None) == "patch"
    return False


def _needs_write_lock(args: argparse.Namespace) -> bool:
    """写命令统一走 book/run/LOCK；只读命令（status / ledger verify / plan validate）不加锁。

    只读诊断不加锁也就不被 active-job fence 拦：现场审计证明
    无人值守长跑期间操作者/审计员需要 `plan validate` 做只读诊断，而旧表把整个
    `plan` 与 `chapter` 命令组都当写命令，`plan validate` / `chapter precheck`
    被 autopilot_fence 拒掉，逼着人用内存探针绕行。写面收窄到真正改状态的子命令。
    """
    cmd = args.cmd
    if cmd == "database":
        return args.database_cmd not in {"verify", "backup"}
    if cmd == "review":
        return args.review_cmd != "list"
    if cmd == "chapter":
        return getattr(args, "chapter_cmd", None) != "precheck"
    if cmd == "plan":
        return getattr(args, "plan_cmd", None) in ("extend", "select-batch", "rebudget", "amend-contract", "patch-chapter")
    if cmd == "retry-authorize":
        return True
    if cmd == "book":
        book_cmd = getattr(args, "book_cmd", None)
        # reopen 也写 HEAD（复位 status/phase），漏了它就有第二个解锁头的写者。
        if book_cmd == "hatch":
            return not (bool(getattr(args, "check_only", False)) or bool(getattr(args, "sample_manifest", False)))
        if book_cmd in ("complete", "close-early", "reopen", "resync-baseline"):
            return True
        return False
    if cmd == "hooks":
        return getattr(args, "hooks_cmd", None) in ("close", "defer", "merge")
    if cmd == "relations":
        return getattr(args, "relations_cmd", None) in ("rename", "close")
    if cmd == "voice":
        return getattr(args, "voice_cmd", None) != "list"
    if cmd == "kb":
        return args.kb_cmd == "sync"
    if cmd == "ledger":
        return args.ledger_cmd == "repair"
    return False


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.cmd == "book" and getattr(args, "book_cmd", None) == "hatch":
        if not getattr(args, "manifest", None) and not getattr(args, "sample_manifest", False):
            raise LedgerError("missing_manifest", "--manifest is required unless --sample-manifest is given")
        if not getattr(args, "sample_manifest", False):
            _prevalidate_hatch(args)
    if not _needs_write_lock(args):
        return _run(args)
    if not getattr(args, "project", None):
        raise LedgerError("missing_project", "--project is required")
    store = BookStore(Path(args.project))
    with store.exclusive_lock():
        if args.cmd == "database" and args.database_cmd == "migrate":
            return _run(args)
        with store.transaction():
            return _run(args)


def _check_submit_file(store: BookStore, args: argparse.Namespace, expected_path) -> None:
    """`draft-submit --file` / `polish-submit --file` 只允许重复该阶段自己的落点。

    这两个子命令本身不需要路径（固定读 staging）。接受 `--file` 纯粹是为了让"先 precheck
    --file 再 submit --file"的直觉写法可用；但必须挡住"指一个任意文件就当提交了"——
    那会让手改过的稿子绕过阶段落点。
    """
    supplied = getattr(args, "file", None)
    if not supplied:
        return
    chapter = int(store.read_head().get("chapter") or 0)
    expected = expected_path(chapter).resolve()
    given = Path(str(supplied)).resolve()
    if given != expected:
        raise LedgerError(
            "wrong_staging_file",
            "this submit always reads its own fixed staging file; --file may only repeat that path",
            {"expected": str(expected), "supplied": str(given)},
        )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.cmd == "database":
        from .database_ops import migrate, export_project, import_source
        store = _project(args)
        if args.database_cmd == "migrate":
            return migrate(store)
        if args.database_cmd == "verify":
            return {"action": "database_verify", **store.database.integrity()}
        if args.database_cmd == "backup":
            return store.database.backup(Path(args.destination))
        if args.database_cmd == "export":
            return export_project(store, Path(args.destination) if args.destination else None)
        return import_source(store, Path(args.source), kind=args.kind)
    if args.cmd == "memory":
        from ..content.recall import recall
        store = _project(args)
        before = args.before_chapter if args.before_chapter is not None else int(store.read_head().get("last_committed_ch") or 0) + 1
        return recall(store, before_chapter=before, people=args.person,
                      topics=args.topic, assets=args.asset, event_ids=args.event_id,
                      query=args.query, limit=args.limit, max_chars=args.max_chars)
    if args.cmd == "review":
        from ..content.reviews import list_findings, resolve_finding
        store = _project(args)
        if args.review_cmd == "list":
            return list_findings(store, chapter=args.chapter, limit=args.limit)
        if args.review_cmd == "resolve":
            return resolve_finding(store, args.id, disposition=args.disposition, actor=args.actor, reason=args.reason)
        if args.review_cmd in {"story-next", "story-submit", "story-evidence"}:
            from ..content.story_review import pending_review, prepare_review, submit_review, export_evidence
            if args.review_cmd == "story-submit":
                return submit_review(store, read_json(Path(args.output)))
            if args.review_cmd == "story-evidence":
                return export_evidence(store, args.chapter)
            pending = pending_review(store, completing=args.complete)
            from .pipeline import _with_execution_contract
            return _with_execution_contract(prepare_review(store, pending), store) if pending else {"ok": True, "action": "story_reviews_current"}
        from .pipeline import ack_patched_chapter
        return ack_patched_chapter(store, chapter=args.chapter, quotes=args.quote)
    if args.cmd == "run":
        from .autopilot import (
            autopilot_status,
            handoff_report,
            resume_run,
            run_checkpoint,
        )

        store = _project(args)
        if args.run_cmd == "status":
            return autopilot_status(store)
        if args.run_cmd == "handoff":
            return handoff_report(store)
        if args.run_cmd == "checkpoint":
            return run_checkpoint(store)
        return resume_run(store)

    if args.cmd == "book" and args.book_cmd == "hatch" and getattr(args, "sample_manifest", False):
        from ..infra.util import ok as _ok

        from .hatch_sample import build_sample_manifest

        return _ok(
            action="hatch_sample_manifest",
            schema="novel-ledger.hatch.v3",
            manifest=build_sample_manifest(),
            hint=(
                "same-source valid sample (its shape is locked end-to-end by tests). Edit the "
                "values to the chosen book, keep the shape, and iterate with "
                "`book hatch --check-only --manifest <file>` — no need to read validator source."
            ),
        )
    if args.cmd == "book" and args.book_cmd == "hatch" and args.check_only:
        from ..infra.util import ok as _ok

        manifest = read_json(Path(args.manifest))
        return _ok(
            action="hatch_check",
            schema=manifest.get("schema", "invalid"),
            guided=manifest.get("schema") == "novel-ledger.hatch.v3",
            message="hatch manifest and voice are valid; no project files were changed",
        )

    # 共享写路守卫：完结的书不接受任何改故事状态的写命令。
    if _mutates_story_state(args):
        from .pipeline import require_story_write_allowed

        require_story_write_allowed(_project(args))
    if args.cmd == "kb" and args.kb_cmd == "extract":
        from ..content.extract import extract_to_file

        return extract_to_file(
            Path(args.source),
            Path(args.out),
            excerpt_max=args.excerpt_max,
        )
    if args.cmd == "kb" and args.kb_cmd == "sync":
        from ..content.extract import extract_to_file

        store = _project(args)
        canon = store.canon_dir
        if not canon.is_dir():
            raise LedgerError("missing_canon", f"canon directory does not exist: {canon}")
        excerpt_max = int(args.excerpt_max or 0)
        return extract_to_file(canon, store.kb_path, excerpt_max=excerpt_max)
    if args.cmd == "context" and args.context_cmd == "read":
        from ..content.narrative_contract import read_narrative_source
        return read_narrative_source(_project(args), args.source, name=args.name, section=args.section)
    if args.cmd == "plan" and args.plan_cmd == "volume-outline":
        from ..infra.volume_outline import volume_outline_key, volume_outline_view
        store = _project(args)
        plan = store.load_plan()
        view = volume_outline_view(plan, args.volume)
        if args.volume is not None and view["selected"] is None:
            raise LedgerError("volume_outline_missing", "requested volume is not in the book plan", {"volume": volume_outline_key(args.volume), "index": view["index"]})
        return ok(action="plan_volume_outline", plan_path=str(store.plan_path), **view)
    if args.cmd == "plan" and args.plan_cmd == "set-spine":
        from .pipeline import set_world_spine

        if bool(getattr(args, "file", None)) == bool(getattr(args, "text", None)):
            raise LedgerError("invalid_args", "set-spine takes exactly one of --file or --text")
        spine_text = (
            Path(args.file).read_text(encoding="utf-8-sig")
            if getattr(args, "file", None)
            else str(getattr(args, "text", "") or "")
        )
        return set_world_spine(
            _project(args), text=spine_text, actor=args.actor, reason=args.reason,
        )
    if args.cmd == "plan" and args.plan_cmd == "amend-contract":
        from ..content.narrative_contract import amend_narrative_contract
        return amend_narrative_contract(_project(args), read_json(Path(args.file)), actor=args.actor, reason=args.reason)
    if args.cmd == "plan" and args.plan_cmd == "rebudget":
        from .pipeline import plan_rebudget
        return plan_rebudget(_project(args), actor=args.actor, reason=args.reason)
    if args.cmd == "plan" and args.plan_cmd == "patch-chapter":
        from .pipeline import plan_patch_chapter
        return plan_patch_chapter(
            _project(args),
            chapter=int(args.chapter),
            patch=read_json(Path(args.patch_file)),
            actor=args.actor,
            reason=args.reason,
        )
    if args.cmd == "plan" and args.plan_cmd == "extend":
        from ..infra.util import ok as _ok

        store = _project(args)
        raw = read_json(Path(args.chapters))
        phase_brief = None
        volumes = None
        character_profiles = None
        if isinstance(raw, dict) and isinstance(raw.get("chapters"), list):
            chapters = raw["chapters"]
            phase_brief = raw.get("phase")
            if raw.get("volumes") is not None:
                volumes = raw["volumes"]
            if raw.get("character_profiles") is not None:
                character_profiles = raw["character_profiles"]
        elif isinstance(raw, dict) and (
            isinstance(raw.get("volumes"), dict)
            or isinstance(raw.get("character_profiles"), dict)
        ):
            # 可以只签卷合同或补人物档案，不改已有章拍。
            chapters = []
            volumes = raw.get("volumes")
            character_profiles = raw.get("character_profiles")
        elif isinstance(raw, list):
            chapters = raw
        else:
            raise LedgerError("invalid_plan", "plan extend input must contain chapters, volumes, or character_profiles")
        result = store.extend_plan(
            chapters, phase_brief=phase_brief, volumes=volumes,
            character_profiles=character_profiles,
        )
        # due 对排期对账：签批即对账——due 抢在新排期引用章之前的当场亮出，
        # 拿着签批结果的人顺手 defer 对齐，不留 6–8 章的预检噪音。
        from .pipeline import hooks_schedule_gaps, overdue_hooks_undeclared

        gaps = hooks_schedule_gaps(store)
        undeclared = overdue_hooks_undeclared(store)
        return _ok(
            action="plan_extend",
            added=result["added"],
            max_chapter=result["max_chapter"],
            phase_id=result.get("phase_id"),
            volumes_updated=result.get("volumes_updated") or [],
            character_profiles_updated=result.get("character_profiles_updated") or [],
            total_chapters=len(store.load_plan().get("chapters") or []),
            warnings=result.get("warnings") or [],
            hooks_schedule_gaps=gaps,
            overdue_hooks_undeclared=undeclared or None,
            hint=(
                "hooks_schedule_gaps: these open hooks are due BEFORE the chapter that first "
                "schedules them via effects.hooks — defer each due to its scheduled_at "
                "(hooks defer --id <id> --due <scheduled_at>) or move the payoff earlier. "
                "overdue_hooks_undeclared: these overdue hooks have no effects.hooks reference "
                "in any unwritten chapter — declare their payoff in the beats, or defer them "
                "explicitly (hooks defer); do not leave them silent"
            )
            if (gaps or undeclared)
            else None,
        )
    if args.cmd == "plan" and args.plan_cmd == "validate":
        from .pipeline import validate_plan

        store = _project(args)
        return validate_plan(store)
    if args.cmd == "plan" and args.plan_cmd == "select-batch":
        from .pipeline import select_batch

        store = _project(args)
        return select_batch(store, path=args.file, actor=args.actor, reason=args.reason)
    if args.cmd == "voice" and args.voice_cmd == "list":
        from ..infra.util import ok as _ok
        from ..voice.voice_manual import list_voices

        items = list_voices()
        return _ok(action="voice_list", voices=items, count=len(items))
    if args.cmd == "voice" and args.voice_cmd == "apply":
        from ..voice.voice_manual import apply_voice

        return apply_voice(_project(args), voice_id=args.voice or None, cap=args.cap)
    if args.cmd == "config" and args.config_cmd == "get":
        cfg_store = _project(args)
        cfg = cfg_store.load_config()
        return ok(
            action="config_get",
            key=args.key,
            value=_dotted_config_get(cfg, args.key) if args.key else cfg,
            storage={"authority": "database", "file_role": "exports_and_inputs"},
            shadow=cfg_store.config_shadow_divergence(),
        )
    if args.cmd == "config" and args.config_cmd == "set":
        cfg_store = _project(args)
        cfg = cfg_store.load_config()
        old = _dotted_config_get(cfg, args.key)
        _dotted_config_set(cfg, args.key, _parse_config_value(args.value))
        cfg_store.save_config(cfg)
        return ok(
            action="config_set",
            key=args.key,
            old=old,
            new=_dotted_config_get(cfg, args.key),
            actor=args.actor,
            storage={"authority": "database"},
            hint=(
                "written through the authoritative channel; effective from the next "
                "command, no restart needed. Direct edits to config.json on disk never "
                "take effect (it is only a projected view)."
            ),
        )
    store = _project(args)
    if args.cmd == "status":
        return status(store, card=bool(getattr(args, "card", False)))
    if args.cmd == "ledger" and args.ledger_cmd == "verify":
        from ..ledger.ledger import verify_ledger
        from ..infra.util import ok as _ok

        return _ok(action="ledger_verify", **verify_ledger(store))
    if args.cmd == "ledger" and args.ledger_cmd == "repair":
        from ..ledger.ledger import repair_ledger
        from ..infra.util import ok as _ok

        return _ok(action="ledger_repair", **repair_ledger(store))
    if args.cmd == "chapter":
        if args.chapter_cmd == "next":
            return chapter_next(store, card=bool(getattr(args, "card", False)))
        if args.chapter_cmd == "usage-record":
            return record_usage(
                store,
                stage=args.stage,
                usage=_usage_from_cli(args),
                chapter=args.chapter,
                request_id=args.request_id,
                session_id=args.session_id or None,
            )
        if args.chapter_cmd == "usage-authorize":
            return authorize_usage_resume(
                store,
                chapter=args.chapter,
                action=args.action,
                actor=args.actor,
                reason=args.reason,
            )
        if args.chapter_cmd == "usage-void":
            from .pipeline import void_usage

            return void_usage(store, request_id=args.request_id, reason=args.reason)
        if args.chapter_cmd == "draft-submit":
            from .pipeline import stage_draft_submit

            _check_submit_file(store, args, store.draft_text_path)
            return stage_draft_submit(store)
        if args.chapter_cmd == "polish-submit":
            from .pipeline import stage_polish_submit

            _check_submit_file(store, args, store.polished_text_path)
            return stage_polish_submit(store)
        if args.chapter_cmd == "submit":
            return submit_output(
                store, stable_read_json(Path(args.output)), force=bool(getattr(args, "force", False))
            )
        if args.chapter_cmd in ("check-submit", "validate-submit"):
            return check_submit_output(store, read_json(Path(args.output)))
        if args.chapter_cmd == "precheck":
            return precheck_prose(store, Path(args.file), chapter=args.chapter)
        if args.chapter_cmd == "ack-read":
            if getattr(args, "chapter", None) is not None:
                head = store.read_head()
                active = int(head.get("chapter") or 0)
                if str(head.get("phase") or "") != "await_ack" or active <= 0:
                    raise LedgerError(
                        "invalid_chapter",
                        "ack-read reviews the ACTIVE chapter and takes no routing: no ack is in "
                        "flight right now; run `chapter next` to see the current action",
                    )
                if int(args.chapter) != active:
                    raise LedgerError(
                        "invalid_chapter",
                        f"ack-read reviews the ACTIVE chapter {active}; got --chapter "
                        f"{int(args.chapter)}. Drop the flag or echo the active chapter number.",
                        {"active_chapter": active},
                    )
            quotes = list(args.quote or [])
            if args.quotes_file:
                loaded = read_json(Path(args.quotes_file))
                if not isinstance(loaded, list):
                    raise LedgerError(
                        "invalid_quotes",
                        "quotes-file must be a JSON list",
                        '文件内容必须是纯 JSON 字符串数组，如 ["第一句逐字引文……", "第二句……"]；'
                        "不要写成对象或带键名的结构",
                    )
                quotes.extend(str(x) for x in loaded)
            return ack_read(
                store,
                quotes=quotes,
                verdict=args.verdict,
                voice_note=args.voice_note or None,
                prose_hash=args.prose_hash or None,
                findings=read_json(Path(args.findings_file)) if args.findings_file else None,
            )
        if args.chapter_cmd == "patch":
            from .pipeline import patch_prose

            patches: list[tuple[str, str]] = []
            if getattr(args, "patch_file", None):
                raw_patches = read_json(Path(args.patch_file))
                if isinstance(raw_patches, dict):
                    raw_patches = [raw_patches]
                if not isinstance(raw_patches, list):
                    raise LedgerError("invalid_patch_file", "patch-file must be a JSON object or list of objects")
                for p in raw_patches:
                    if not isinstance(p, dict) or "target" not in p or "replacement" not in p:
                        raise LedgerError("invalid_patch_item", "each patch must contain 'target' and 'replacement'")
                    patches.append((str(p["target"]), str(p["replacement"])))
            elif getattr(args, "target", None):
                patches.append((args.target, args.replacement))
            else:
                raise LedgerError("missing_patch_arguments", "either --target and --replacement or --patch-file is required")

            return patch_prose(
                store,
                patches=patches,
                chapter=args.chapter,
                bypass_style_check=args.bypass_style_check,
            )
        if args.chapter_cmd == "rework-patch":
            from .pipeline import rework_patch

            rework_patches: list[tuple[str, str]] = []
            if getattr(args, "patch_file", None):
                raw_rework = read_json(Path(args.patch_file))
                if isinstance(raw_rework, dict):
                    raw_rework = [raw_rework]
                if not isinstance(raw_rework, list):
                    raise LedgerError("invalid_patch_file", "patch-file must be a JSON object or list of objects")
                for p in raw_rework:
                    if not isinstance(p, dict) or "target" not in p or "replacement" not in p:
                        raise LedgerError("invalid_patch_item", "each patch must contain 'target' and 'replacement'")
                    rework_patches.append((str(p["target"]), str(p["replacement"])))
            elif getattr(args, "target", None):
                rework_patches.append((args.target, args.replacement))
            else:
                raise LedgerError("missing_patch_arguments", "either --target and --replacement or --patch-file is required")

            return rework_patch(
                store,
                patches=rework_patches,
                bypass_style_check=args.bypass_style_check,
            )
    if args.cmd == "retry-authorize":
        return retry_authorize(store, actor=args.actor, reason=args.reason, action=args.action)

    if args.cmd == "relations":
        from .pipeline import close_relation, rename_relation

        if args.relations_cmd == "rename":
            return rename_relation(
                store, from_name=args.from_name, to_name=args.to_name,
                actor=args.actor, reason=args.reason,
            )
        if args.relations_cmd == "close":
            return close_relation(
                store,
                who=args.who,
                target=args.target,
                kind_substring=args.kind,
                reason=args.reason,
                actor=args.actor,
            )
        raise LedgerError("invalid_args", f"unknown relations_cmd: {args.relations_cmd}")
    if args.cmd == "book":
        if args.book_cmd == "audit":
            return audit_book(store)
        if args.book_cmd == "reconcile":
            from .pipeline import reconcile_book

            return reconcile_book(store)
        if args.book_cmd == "calibrate":
            from .pipeline import calibrate_book

            return calibrate_book(store)
        if args.book_cmd == "facts":
            from .pipeline import book_facts

            return book_facts(store)
        if args.book_cmd == "close-early":
            return book_close_early(_project(args), actor=args.actor, reason=args.reason, author_confirmed=args.author_confirmed)
        if args.book_cmd == "complete":
            return book_complete(
                store,
                actor=args.actor,
                reason=args.reason,
                override_target=bool(args.override_target),
            )
        if args.book_cmd == "reopen":
            return book_reopen(store, actor=args.actor, reason=args.reason)
        if args.book_cmd == "resync-baseline":
            from .pipeline import resync_baseline

            return resync_baseline(
                store,
                fix_quotes=not args.no_fix_quotes,
                restamp_canon=bool(getattr(args, "restamp_canon", False)),
            )
        if args.book_cmd == "hatch":
            from .hatch import hatch_project

            return hatch_project(
                Path(args.project),
                read_json(Path(args.manifest)),
            )
    if args.cmd == "hooks":
        from .pipeline import audit_hooks, close_hook, defer_hook, merge_hook

        if args.hooks_cmd == "audit":
            return audit_hooks(store)
        if args.hooks_cmd == "close":
            return close_hook(store, hook_id=args.id, reason=args.reason, actor=args.actor)
        if args.hooks_cmd == "defer":
            return defer_hook(
                store, hook_id=args.id, new_due=args.due, actor=args.actor, reason=args.reason,
                force=bool(getattr(args, "force", False)),
            )
        if args.hooks_cmd == "merge":
            return merge_hook(
                store, from_id=args.from_id, into_id=args.into_id,
                actor=args.actor, reason=args.reason,
            )
    raise LedgerError("unknown_command", f"unknown command: {args.cmd}")
