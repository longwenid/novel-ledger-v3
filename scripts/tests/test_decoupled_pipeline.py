"""解耦流水线（单一 compact 档）端到端回归。

锁定的阶段不变量：
- action 顺序必须是 draft → polish → assemble → submit；
- 情节自检由事实编辑在组装时兼做（plot_self_check），文风硬红线由 polish 机检收口；
- 阶段一产出纯文本草稿；阶段二产出纯文本终稿；阶段三才产出 submit JSON；
- submit 时 prose 必须与阶段二终稿逐字一致（assembly 不得改文）；
- 机检失败后回到 await_draft，允许整链重跑一次。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.conftest import patch_pipeline_name

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    _corpus_style_fingerprint,
    _ack_action,
    _quality_summary,
    ack_read,
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json


def _project(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "title": "",
                "protagonist": "主角",
                "chapters": [
                    {
                        "chapter": 1,
                        "location": "市集",
                        "present": ["主角", "掌柜"],
                        "beats": [
                            {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                            {"id": "b2", "required": True, "text": "凭据仍被扣", "must": "凭据"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(
        proj,
        plan_path=plan_path,
        protagonist="主角",
        word_min=20,
        word_max=5000,
    )
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["polish"] = "on"  # 本文件测润色泳线；skill 默认只写作（polish=off）
    store.save_config(cfg)
    return proj


def _polished(store: BookStore, chapter: int = 1) -> str:
    return (store.polished_text_path(chapter).read_text(encoding="utf-8")).rstrip("\n")


def _assemble_output(store: BookStore, prose: str, chapter: int = 1) -> dict:
    view = read_json(store.assemble_pack_path(chapter))
    return {
        "prose": prose,
        "l1_summary": "主角在市集拒收改期，凭据仍被扣。",
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "主角拒收改期，凭据仍被扣。"}],
            "debts": [],
            "hooks": [],
            "relations": [],
            "named": ["主角", "掌柜"],
            "new_names": [],
            "deaths": [],
        },
        "memory": {"voice_concepts": []},
        "pack_hash": view["pack_hash"],
        "beats_hit": ["b1", "b2"],
    }


def test_ack_action_advertises_configured_quote_contract(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["ack_quotes_min"] = 4
    store.save_config(cfg)
    head = {"chapter": 1, "pack_hash": "sha256:test", "prose_hash": "sha256:prose"}

    ack = _ack_action(store, head)

    expected = {
        "minimum": 4,
        "minimum_chars": 6,
        "require_latter_half_when_chars_at_least": 800,
    }
    assert ack["quote_requirements"] == expected
    assert "≥4 verbatim quotes" in ack["hint"]
    assert "assembly plot self-check plus polish machine/content gates" in ack["hint"]


def test_decoupled_chain_runs_draft_polish_assemble_submit(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)

    r1 = chapter_next(store)
    assert r1["action"] == "draft" and r1["phase"] == "await_draft"
    draft = "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。"
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    assert main(["chapter", "draft-submit", "--project", str(proj)]) == 0

    r2 = chapter_next(store)
    assert r2["action"] == "polish" and r2["phase"] == "await_polish"
    polished = draft  # 内容不动，只有文风才允许变；测试里视为已润色
    Path(r2["polished_output_path"]).write_text(polished, encoding="utf-8")
    assert main(["chapter", "polish-submit", "--project", str(proj)]) == 0

    r3 = chapter_next(store)
    assert r3["action"] == "assemble" and r3["phase"] == "await_assembly"
    output = _assemble_output(store, _polished(store))
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"

    commit = chapter_next(store)
    assert commit["action"] == "ack"
    assert commit["phase"] == "await_ack"
    assert "voice_skill_manual" not in commit  # decoupled 已按域审计，ack 不再带手册防二次混审


def test_content_failure_rewrites_from_draft(tmp_path: Path):
    """must/内容类失败（beat_token_missing）不能靠重组装修复，必须回到正文草稿。"""
    proj = _project(tmp_path)
    store = BookStore(proj)

    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(
        "主角和掌柜商量改期的事，只字不提凭据，说改天再谈。", encoding="utf-8"
    )
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(
        "主角和掌柜商量改期的事，只字不提凭据，说改天再谈。", encoding="utf-8"
    )
    stage_polish_submit(store)
    chapter_next(store)

    bad = _assemble_output(store, _polished(store))
    result = submit_output(store, bad)
    assert result["verdict"] == "rewrite"
    assert result["phase"] == "await_draft"
    assert any(v["code"] == "beat_token_missing" for v in result["violations"])
    assert not store.draft_text_path(1).exists()
    assert not store.polished_text_path(1).exists()


def test_stage_commands_reject_wrong_phase(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    with pytest.raises(LedgerError):
        stage_polish_submit(store)


def test_ack_quotes_need_latter_half_for_long_chapters(tmp_path: Path):
    """长稿的 ack 引用不能全摘前半段：至少一条必须落在后半段，证明确实通读了全文。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    long_prose = (
        "主角站在市集，对掌柜说拒收改期。凭据还压在柜台。改天再来取。"
        + "月色压街，油灯晃。掌柜把凭据又按回柜底，说明早再谈。" * 100
    )
    assert len(long_prose) >= 800

    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(long_prose, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(long_prose, encoding="utf-8")
    stage_polish_submit(store)

    first_half_quotes = ["主角站在市集，对掌柜说拒收改期。", "凭据还压在柜台。", "改天再来取。"]
    r3 = chapter_next(store)
    assert r3["action"] == "assemble"
    pack = read_json(store.current_pack_path)
    output = {
        "prose": long_prose,
        "l1_summary": "主角拒收改期，凭据仍被压着。",
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "拒收改期"}],
            "debts": [],
            "hooks": [],
            "relations": [],
            "named": ["主角", "掌柜"],
            "new_names": [],
            "deaths": [],
        },
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1", "b2"],
    }
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"

    committed = chapter_next(store)
    assert committed["action"] == "ack"
    with pytest.raises(LedgerError) as exc2:
        ack_read(store, quotes=first_half_quotes, verdict="pass")
    assert exc2.value.code == "ack_quotes_not_distributed"
    acked = ack_read(
        store,
        quotes=["掌柜把凭据又按回柜底，说明早再谈。", "凭据还压在柜台。", "改天再来取。"],
        verdict="pass",
    )
    assert acked["verdict"] == "acked"


def test_polish_rewrite_may_reshape_length_without_being_rejected(tmp_path: Path):
    """润色允许“大刀阔斧”重构：不再以草稿 90% 字数作硬性拒收闸门。

    内容删减的取舍由文风审校与剧情/终审闸门负责，不是在此用字数硬拦。

    注意夹具必须**仍然带住 must 词**（拒收/凭据）：本用例要证明的是"字数可以大幅缩短"，
    不是"可以丢掉锚点"。原先用「太短了。」当极端短稿，顺带把 must 词也丢了——
    那是另一条规则（锚点兑现），现在由 polish 收口的锚点闸门拦下，与本用例无关。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    draft = "主角在市集说拒收改期。凭据还压在柜台。掌柜冷笑一声。夜色压下来。" * 4
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    # 大幅缩短但仍在字数带内（word_band 在 polish 收口也是硬闸），且锚点仍在：拒收 / 凭据
    Path(r2["polished_output_path"]).write_text(
        "拒收。凭据还在柜里，掌柜没再拦，夜色压下来，改天再来。", encoding="utf-8"
    )
    result = stage_polish_submit(store)
    assert result["verdict"] == "polish_accepted"
    assert store.read_head()["phase"] == "await_assembly"


def _style_hard_bad_polish() -> str:
    return "主角在市集说了几句收凭据的话。" * 8 + "值得注意的是，这事还没完；明早再说。"


# --------------------------------------------------------------------------- #
# 润色收口的内容锚点闸门（polish-anchor gate）
#
# 这三样（beats/must 兑现、连续性防失忆、glossary 术语归一化）此前只在 `chapter submit`
# 才被机检，而 submit 对正文类问题按约定回 `draft` 整链重写。润色终稿是入账前最后一个
# 动正文的产物，若锚点是**润色自己改坏的**，就该在 polish 收口拦下、只回 polish 重润。
#
# 闸门是纯增量的：草稿本来就有问题的锚点不在这里抢先处理（仍由 submit 回 draft），
# 所以下面既有「润色改坏 → 回 polish」，也有「草稿就坏 → 不拦」两类断言。
# --------------------------------------------------------------------------- #

_DRAFT_WITH_ANCHORS = "主角在市集说拒收改期。凭据还压在柜台。掌柜冷笑一声。夜色压下来。"


def _advance_to_polish(tmp_path: Path) -> tuple[BookStore, Path]:
    """推进到 polish 阶段；草稿是**带全锚点**的干净稿。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(_DRAFT_WITH_ANCHORS * 3, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    assert r2["action"] == "polish", r2
    return store, Path(r2["polished_output_path"])


def test_polish_anchor_gate_bounces_dropped_must_word_back_to_polish(tmp_path: Path):
    """润色把 must 词改丢了 → 在 polish 收口就地拦下，只回 polish 重润。

    这是本闸门存在的理由：此前要等 `submit` 才发现，然后按正文类问题回 `draft`
    整链重写（重写草稿 → 重润 → 重组装），而真正该修的只是润色稿。
    """
    store, polished = _advance_to_polish(tmp_path)
    # 草稿带 拒收/凭据，润色稿丢掉了「凭据」
    polished.write_text("主角在市集说了几句拒收的话。掌柜冷笑一声。夜色压下来。", encoding="utf-8")
    result = stage_polish_submit(store)
    assert result["verdict"] == "polish_anchor_failed"
    assert result["phase"] == "await_polish"
    codes = {issue["code"] for issue in result["issues"]}
    assert "beat_token_missing" in codes
    head = store.read_head()
    assert head["polish_anchor_retry"] == 1
    # 问题清单落盘，且下一次 next 把路径交给全新的文风编辑
    anchors_path = Path(result["polish_anchor_path"])
    assert anchors_path.exists()
    assert read_json(anchors_path)["issue_count"] == len(result["issues"])
    # 上一版润色稿保留在原路径：must 词丢失通常只差几个词，写者可原地补锚点后重交，
    # polish-submit 每次都重新逐字校验锚点，不存在绕过机检的空间
    assert polished.exists()
    assert result.get("polished_output_path") == str(polished)
    nxt = chapter_next(store)
    assert nxt["action"] == "polish"
    assert nxt["polish_anchor_path"] == str(anchors_path)
    # hint 只点字段名（与 style_metrics_path 的既有写法一致），路径本身走上面那个字段
    assert "polish_anchor_path" in nxt["hint"]


def test_polish_anchor_gate_blocks_after_retry_limit(tmp_path: Path):
    """重润满额仍不过 → blocked（不是无限循环），交总编辑裁决。

    满额这一档必须存在：锚点缺失也可能是**草稿侧**的问题（这里用「一直改不好」模拟），
    无人值守下不能靠反复重润空转。
    """
    store, polished = _advance_to_polish(tmp_path)
    bad = "主角在市集说了几句拒收的话。掌柜冷笑一声。夜色压下来。"  # 始终丢「凭据」
    limit = int(store.load_config()["polish_anchor_limit"])
    assert limit >= 1
    seen = []
    for _ in range(limit):
        polished.write_text(bad, encoding="utf-8")
        outcome = stage_polish_submit(store)
        seen.append(outcome["verdict"])
        if outcome["verdict"] == "blocked":
            break
        # 回 polish 后重新拿到输出路径
        polished = Path(chapter_next(store)["polished_output_path"])
    assert seen[-1] == "blocked", seen
    head = store.read_head()
    assert head["phase"] == "blocked"
    assert head["blocked"]["reason"] == "polish_anchor_failed"
    assert head["polish_anchor_retry"] == limit


def test_polish_anchor_gate_leaves_draft_origin_issues_to_submit(tmp_path: Path):
    """草稿本就缺锚点时，polish 收口**不抢** submit 的分流。

    这是闸门"纯增量"的边界：draft-origin 的问题由既有 submit 机检回 draft 整链重写，
    语义与返工预算都不变；polish 收口只接住"润色自己改坏的"那一类。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    # 草稿就丢掉了 must 词「凭据」——问题源自草稿（字数仍在带内）
    dirty = "主角在市集说拒收改期。夜色压下来，掌柜收摊，改天再来对账。"
    Path(r1["draft_output_path"]).write_text(dirty, encoding="utf-8")
    stage_draft_submit(store)
    polished = Path(chapter_next(store)["polished_output_path"])
    polished.write_text(dirty, encoding="utf-8")  # 润色照原样保留，没有新增问题
    result = stage_polish_submit(store)
    assert result["verdict"] == "polish_accepted", result
    assert store.read_head()["phase"] == "await_assembly"


def test_polish_anchor_gate_catches_term_introduced_by_polish(tmp_path: Path):
    """润色引入非规范术语（glossary 违禁词）→ 在 polish 收口拦下。

    glossary 由 `gates._glossary_issues` 在 submit 时强制，而润色是最后一个动正文的阶段：
    它既可能误留、也可能引入违禁词。草稿干净时这里就能拦住，不必等到组装。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["glossary"] = {"压柜": "压在柜台"}
    store.save_config(cfg)
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(_DRAFT_WITH_ANCHORS * 3, encoding="utf-8")
    stage_draft_submit(store)
    polished = Path(chapter_next(store)["polished_output_path"])
    polished.write_text(
        "主角在市集说拒收改期。凭据压柜了。掌柜冷笑一声。夜色压下来。", encoding="utf-8"
    )
    result = stage_polish_submit(store)
    assert result["verdict"] == "polish_anchor_failed", result
    assert {i["code"] for i in result["issues"]} == {"glossary_term_banned"}


def test_style_distribution_deviation_is_directional_not_blocking(tmp_path: Path):
    """节奏分布偏基线只返回方向提示，不能成为逐章硬闸（避免打地鼠）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    r1 = chapter_next(store)
    draft = "主角在市集说了几句收凭据的话。" * 8
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(draft, encoding="utf-8")
    passed = stage_polish_submit(store)
    assert passed["verdict"] == "polish_accepted"
    assert passed["phase"] == "await_assembly"
    assert passed["style_warnings_count"] > 0, "off-target 分布应作为方向提示计数返回"


def test_polish_reports_nonblocking_style_advisories(tmp_path: Path, monkeypatch):
    """即使无分布画像提示，分号和连接词的 advisory 仍进入流水线计数。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    draft = "主角在市集拒收改期，凭据仍压在柜台。" * 8
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(
        draft + "与此同时，掌柜翻了一页账；紧接着又问价。"
        "与此同时，伙计端来一碗粥；掌柜又拿起笔；", encoding="utf-8"
    )
    patch_pipeline_name(monkeypatch, "style_direction", lambda _text: [])
    passed = stage_polish_submit(store)
    assert passed["verdict"] == "polish_accepted"
    assert passed["style_warnings_count"] >= 2


def test_style_hard_gate_rejects_and_hands_off_list(tmp_path: Path):
    """明确出戏的话术仍拦截并落盘交给润色，主任务只拿路径。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    r1 = chapter_next(store)
    draft = "主角在市集说了几句收凭据的话。" * 8
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(_style_hard_bad_polish(), encoding="utf-8")
    rejected = stage_polish_submit(store)
    assert rejected["verdict"] == "style_metrics_failed"
    assert rejected["phase"] == "await_polish"
    assert rejected["fail_count"] > 0
    assert rejected["style_metrics_retry"] == 1
    assert rejected["style_metrics_limit"] == 2
    metrics_path = Path(rejected["style_metrics_path"])
    assert metrics_path.exists()
    assert metrics_path.read_text(encoding="utf-8").strip()
    head = read_json(store.head_path)
    assert head["style_metrics_retry"] == 1
    assert head["style_metrics_pending"] is True
    # 下次 polish 把清单路径交给新文风编辑，主任务无需复述文风条目
    repolish = chapter_next(store)
    assert repolish["action"] == "polish"
    assert repolish["style_metrics_path"] == str(metrics_path)


def test_style_hard_gate_blocks_after_retry_limit(tmp_path: Path):
    """硬红线返工满额必须 blocked，不能无人值守无限“重写→重测”。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    draft = "主角在市集说了几句收凭据的话。" * 8

    def _drive_to_metrics_failure() -> dict:
        action = chapter_next(store)
        if action["action"] == "draft":
            Path(action["draft_output_path"]).write_text(draft, encoding="utf-8")
            stage_draft_submit(store)
            action = chapter_next(store)
        assert action["action"] == "polish"
        Path(action["polished_output_path"]).write_text(_style_hard_bad_polish(), encoding="utf-8")
        return stage_polish_submit(store)

    first = _drive_to_metrics_failure()
    assert first["verdict"] == "style_metrics_failed"
    second = _drive_to_metrics_failure()
    assert second["verdict"] == "blocked"
    assert second["phase"] == "blocked"
    assert second["blocked"]["reason"] == "style_metrics_failed"
    assert second["blocked"]["style_metrics_retry"] == 2
    assert Path(second["style_metrics_path"]).exists()
    assert read_json(store.head_path)["phase"] == "blocked"


def test_cumulative_style_fingerprint_aggregates_committed_chapters(tmp_path: Path):
    """节奏分布只在累计层比对：单章偏差不再拦截，样本不足则跳过。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    prose = "主角在市集说了几句收凭据的话。掌柜把凭据压在柜台，说明早再来取。"
    for ch in range(1, 6):
        path = store.chapter_md_path(ch)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prose + "\n", encoding="utf-8")

    below_min = _corpus_style_fingerprint(store, min_chapters=10)
    assert below_min["skipped"] is True
    assert below_min["reason"] == "not_enough_chapters"

    fp = _corpus_style_fingerprint(store, min_chapters=5)
    assert fp["skipped"] is False
    assert fp["chapters"] == 5
    assert fp["hard_fails"] == []
    assert fp["distribution_fails"], "累计层仍用完整节奏画像比对基线"
    assert fp["ok"] is True
    assert fp["fails"] == [], "统计偏离不得伪装成文风硬失败"


def test_pipeline_has_no_audit_roles(tmp_path: Path):
    """draft-submit 后直接 polish，polish-submit 后直接组装，不出现两位审校角色。"""
    proj = _project(tmp_path)
    store = BookStore(proj)

    r1 = chapter_next(store)
    assert r1["action"] == "draft"
    draft = "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。"
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    accepted_draft = stage_draft_submit(store)
    assert accepted_draft["phase"] == "await_polish"

    r2 = chapter_next(store)
    assert r2["action"] == "polish"
    Path(r2["polished_output_path"]).write_text(draft, encoding="utf-8")
    accepted_polish = stage_polish_submit(store)
    assert accepted_polish["verdict"] == "polish_accepted"
    assert accepted_polish["phase"] == "await_assembly"
    assert isinstance(accepted_polish["style_warnings_count"], int)

    r3 = chapter_next(store)
    assert r3["action"] == "assemble"
    output = _assemble_output(store, _polished(store))
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"
    assert store.read_head()["phase"] == "submitted"
    summary = _quality_summary(store)
    assert summary["begins"] == 1
    assert summary["style_machine_pass"] == 1
    assert summary["submit_accepted"] == 1
    log_text = store.quality_log_path.read_text(encoding="utf-8")
    assert "plot_audit" not in log_text
    assert "style_audit" not in log_text


def test_style_hard_gate_loops_to_polish_then_blocks(tmp_path: Path):
    """无独立文风审校；文风硬红线机检在 polish-submit 后原地收口。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    draft = "主角在市集说了几句收凭据的话。" * 8

    def _drive() -> dict:
        action = chapter_next(store)
        if action["action"] == "draft":
            Path(action["draft_output_path"]).write_text(draft, encoding="utf-8")
            stage_draft_submit(store)
            action = chapter_next(store)
        assert action["action"] == "polish"
        Path(action["polished_output_path"]).write_text(_style_hard_bad_polish(), encoding="utf-8")
        return stage_polish_submit(store)

    first = _drive()
    assert first["verdict"] == "style_metrics_failed"
    assert first["phase"] == "await_polish"
    assert first["style_metrics_retry"] == 1
    assert read_json(store.head_path)["style_metrics_pending"] is True

    second = _drive()
    assert second["verdict"] == "blocked"
    assert second["phase"] == "blocked"
    assert second["blocked"]["reason"] == "style_metrics_failed"
    assert second["blocked"]["style_metrics_retry"] == 2
    summary = _quality_summary(store)
    assert summary["style_machine_fail"] == 2


_PLACEHOLDER = "主角在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。"


def _drive_to_assembly(store: BookStore, draft: str) -> dict:
    """compact：draft → polish → 组装。返回组装响应。"""
    r1 = chapter_next(store)
    assert r1["action"] == "draft", r1
    assert r1.get("plot_self_check") is None  # 草稿阶段不注入情节自检
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    assert r2["action"] == "polish", r2
    Path(r2["polished_output_path"]).write_text(draft, encoding="utf-8")
    stage_polish_submit(store)
    r3 = chapter_next(store)
    assert r3["action"] == "assemble", r3
    return r3


def test_compact_assemble_carries_plot_self_check(tmp_path: Path):
    """compact 档给事实编辑下发情节自检清单；full 档不发（已有独立剧情审校）。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    r3 = _drive_to_assembly(store, _PLACEHOLDER)
    checks = r3.get("plot_self_check")
    assert isinstance(checks, list) and checks
    joined = "\n".join(checks)
    assert "世界规则与人物边界" in joined and "拍点" in joined
    assert "人设与行为一致性" in joined
    assert "首读定位与转场" in joined
    assert "章内局部变化与人物反应" in joined


def test_compact_plot_self_check_empty_passes(tmp_path: Path):
    """plot_findings 为空数组 → 正常接受，不影响流水线。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["plot_findings"] = []
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"


def test_compact_plot_self_check_true_finding_rewrites_from_draft(tmp_path: Path):
    """BLOCKER（quote 逐字合法）→ 回草稿重写，消耗 rewrite 配额。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    polished = _polished(store)
    # 从终稿里取一句真实连续子串作 quote（≥6 字）
    quote = polished[10:30]
    output = _assemble_output(store, polished)
    output["plot_findings"] = [
        {
            "code": "beat_missing",
            "severity": "BLOCKER",
            "hint": "本章必发生的凭据场景没兑现",
            "quote": quote,
        }
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "plot_fix"
    assert res["phase"] == "await_draft"
    assert res["rewrite_count"] == 1
    assert res["findings"][0]["code"] == "beat_missing"
    assert not store.polished_text_path(1).exists()  # 终稿已丢弃，待重写


def test_compact_plot_self_check_hallucinated_quote_returns_to_assembly(tmp_path: Path):
    """quote 不在终稿（幻觉证据）→ 拒收，回组装重跑，不误伤正文。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["plot_findings"] = [
        {
            "code": "x",
            "severity": "BLOCKER",
            "hint": "捏造的问题",
            "quote": "终稿里根本没有的这一句",
        }
    ]
    res = submit_output(store, output)
    assert res["verdict"] == "fix_assembly"
    assert res["phase"] == "await_assembly"
    assert any(v["code"] == "plot_finding_quote_not_in_prose" for v in res["violations"])
    assert store.polished_text_path(1).exists()  # 只重组装，终稿保留


def test_compact_plot_self_check_missing_hint_rejected(tmp_path: Path):
    """plot_findings 条目缺 hint → 组装契约错误，回组装。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["plot_findings"] = [{"code": "x", "severity": "BLOCKER"}]
    res = submit_output(store, output)
    assert res["verdict"] == "fix_assembly"
    assert res["phase"] == "await_assembly"
    assert any(v["code"] == "plot_finding_missing_hint" for v in res["violations"])


def test_compact_plot_self_check_requires_severity(tmp_path: Path):
    """severity 缺失属于组装契约错误；由 submit 原地修复，不弹回正文。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    output = _assemble_output(store, _polished(store))
    output["plot_findings"] = [{"code": "x", "hint": "缺 severity"}]
    res = submit_output(store, output)
    assert res["verdict"] == "fix_assembly"
    assert res["phase"] == "await_assembly"
    assert any(v["code"] == "plot_finding_severity_invalid" for v in res["violations"])


def test_compact_plot_self_check_warning_does_not_rewrite(tmp_path: Path):
    """WARNING 记录但放行；只有 BLOCKER 才换走一章的草稿与终稿。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    store.save_config(cfg)
    _drive_to_assembly(store, _PLACEHOLDER)
    polished = _polished(store)
    output = _assemble_output(store, polished)
    output["plot_findings"] = [
        {
            "code": "minor_ambiguity",
            "severity": "WARNING",
            "hint": "场景衔接略突兀，但不构成硬伤",
            "quote": polished[10:30],
        }
    ]
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"
    assert any(w.get("source") == "plot_self_check" for w in accepted["warnings"])
    assert store.polished_text_path(1).exists()


def _polished_placeholder() -> str:
    return _PLACEHOLDER


def test_glossary_definition_entry_does_not_ban_canon_term(tmp_path: Path):
    """glossary 被填成「词条→释义」时，机检不得把键当违禁词拦正文。

    值像释义句（含标点/超长/释义腔标记）→ 判定误用、跳过拦截——
    配置错误归 book audit 的 glossary_misuse 点名，不进章内返工循环。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["glossary"] = {"贝克教区": "旧城东的教区，贝克家族的世袭领地"}
    store.save_config(cfg)
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(_DRAFT_WITH_ANCHORS * 3, encoding="utf-8")
    stage_draft_submit(store)
    polished = Path(chapter_next(store)["polished_output_path"])
    polished.write_text(
        "主角在市集说拒收改期。贝克教区的凭据还压着。掌柜冷笑一声。夜色压下来。", encoding="utf-8"
    )
    result = stage_polish_submit(store)
    assert result["verdict"] != "polish_anchor_failed" or not [
        i for i in result.get("issues", []) if i.get("code") == "glossary_term_banned"
    ], result


def test_glossary_key_in_kb_card_is_skipped(tmp_path: Path):
    """键本身就是知识库卡里的正典词 → 自相矛盾，判误用跳过。"""
    from novel_ledger_core.content.gates import _glossary_entry_misused

    pack = {"kb_slice": [{"id": "brief-place", "title": "黑皮笔记", "excerpt": "贯穿全书的线索物"}]}
    assert _glossary_entry_misused("黑皮笔记", "线索物", pack) is True
    assert _glossary_entry_misused("黑皮手册", "黑皮笔记", pack) is False


def test_book_audit_flags_glossary_misuse(tmp_path: Path):
    """book audit 把误用条目逐条点名：值像释义 / 键是正典词，并指路修正。"""
    from novel_ledger_core.control.pipeline import audit_book

    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["glossary"] = {
        "贝克教区": "旧城东的教区，贝克家族的世袭领地",  # 值像释义句
        "档案馆": "档案局",                              # 正常变体收编，不该报
    }
    store.save_config(cfg)
    report = audit_book(store)
    misuse = report.get("glossary_misuse") or []
    assert len(misuse) == 1, misuse
    assert misuse[0]["wrong_term"] == "贝克教区"
    assert misuse[0]["reasons"]
    assert "不是释义词典" in misuse[0]["hint"]


def _advance_to_assemble(tmp_path: Path) -> BookStore:
    """推进到 await_assembly：草稿与终稿均已过闸、正等组装提交。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    Path(r1["draft_output_path"]).write_text(_DRAFT_WITH_ANCHORS * 3, encoding="utf-8")
    stage_draft_submit(store)
    r2 = chapter_next(store)
    Path(r2["polished_output_path"]).write_text(_DRAFT_WITH_ANCHORS * 3, encoding="utf-8")
    stage_polish_submit(store)
    chapter_next(store)
    return store


def test_stale_pack_with_unchanged_views_keeps_staged_prose(tmp_path: Path):
    """上游变化但本章视图未变（kb 加无关卡）→ 不得作废已过闸的草稿/终稿。

    此前任何指纹变化都整章回 draft 清空两份 staging；现在视图逐层比对，
    视图未变时 submit 换新 pack 继续走（组装件补对 hash 即可），正文零重写。
    """
    store = _advance_to_assemble(tmp_path)
    kb = read_json(store.kb_path)
    kb["cards"] = list(kb.get("cards") or []) + [
        {"id": "far-island", "kind": "geography", "title": "远方孤岛", "excerpt": "与本章拍点无关的设定"}
    ]
    store.kb_path.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    result = submit_output(store, _assemble_output(store, _polished(store)))
    assert result["phase"] != "await_draft", result
    assert store.draft_text_path(1).exists(), "无关上游变化不得清掉已过闸草稿"
    assert store.polished_text_path(1).exists(), "无关上游变化不得清掉已过闸终稿"


def test_stale_pack_with_changed_beats_still_resets_chapter(tmp_path: Path):
    """本章拍点真变了 → draft 视图变 → 维持整章重置语义（现状不回退）。"""
    store = _advance_to_assemble(tmp_path)
    plan = store.load_plan()
    plan["chapters"][0]["beats"].append(
        {"id": "b3", "required": True, "text": "新拍点压上来", "must": "新词"}
    )
    store.save_plan(plan)
    result = submit_output(store, _assemble_output(store, _polished(store)))
    assert result["verdict"] == "stale_pack", result
    assert result["phase"] == "await_draft"
    assert not store.draft_text_path(1).exists()
    assert not store.polished_text_path(1).exists()


def test_refresh_stage_views_scope_polish_keeps_draft(tmp_path: Path):
    """scope=polish 只清润色层产物、保留草稿；scope=draft 维持全清。"""
    from novel_ledger_core.control.pipeline import _refresh_stage_views

    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)  # begin ch1 → current pack 落盘
    draft = store.draft_text_path(1)
    draft.write_text("旧草稿", encoding="utf-8")
    pack = read_json(store.current_pack_path)
    _refresh_stage_views(store, pack, 1, scope="polish")
    assert draft.exists()
    _refresh_stage_views(store, pack, 1, scope="none")
    assert draft.exists()
    _refresh_stage_views(store, pack, 1, scope="draft")
    assert not draft.exists()


def test_plan_validate_flags_must_word_locking_with_glossary(tmp_path: Path):
    """must 命中 glossary 被禁旧写法 = 终稿必须含又必须不含同一词,章内无解。"""
    from novel_ledger_core.control.pipeline import validate_plan

    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["glossary"] = {"账簿": "账册"}
    store.save_config(cfg)
    plan = store.load_plan()
    plan["chapters"][0]["beats"].append(
        {"id": "b3", "required": True, "text": "以黑市账簿作交换", "must": "账簿"}
    )
    store.save_plan(plan)
    report = validate_plan(store)
    codes = {w.get("code") for w in report.get("warnings") or []}
    assert "must_word_is_banned_term" in codes, report
