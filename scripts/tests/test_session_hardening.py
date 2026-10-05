"""会话期硬化项回归（源自长连写会话的问题复盘）。

覆盖：
- `chapter precheck`：交稿前官方自检（字数带 + 文风硬红线，只读不耗配额）；
- draft/polish 任务书前置字数缺口（word_band / draft_chars / chars_to_min）；
- submit 原子机检：组装错误留在本阶段且不消耗正文返工额度；
- 润色机检失败后润色稿保留（polished_kept），不再静默删文件逼整篇重写；
- session_mode=inline-compact：ack 后 requires_new_session=false。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import (
    ack_read,
    chapter_next,
    check_submit_output,
    precheck_prose,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, chinese_word_count, read_json
from tests.decoupled_helpers import advance_to_assembly

PROSE = "主角站在市集，对掌柜说拒收改期。凭据还压在柜台那头，改天再来取。"
# 25 汉字/次 × 110 ≈ 2750：含 must 词（拒收/凭据）、无禁用字符，用于越过 2500 下限
FILLER_SCENE = "掌柜把凭据压回柜台，说年关再谈。主角拒收改期，改天再来取。"


def _project(tmp_path: Path, *, word_min: int = 20) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=word_min, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["polish"] = "on"  # 本文件测润色泳线；skill 默认只写作
    store.save_config(cfg)
    return proj


def _output(store: BookStore, prose: str) -> dict:
    view = read_json(store.assemble_pack_path(1))
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


def _quotes(prose: str) -> list[str]:
    return ["对掌柜说拒收改期", "凭据还压在柜台那头", "改天再来取。"]


def test_precheck_reports_shortfall_and_meta_narration(tmp_path: Path):
    proj = _project(tmp_path, word_min=2500)
    store = BookStore(proj)
    target = tmp_path / "draft-0001.txt"
    target.write_text("主角拒收改期；凭据仍被扣。", encoding="utf-8")

    short = precheck_prose(store, target, chapter=1)
    assert short["ok"] and short["verdict"] == "fail"
    assert short["chars"] < 2500
    assert any("word_band.min" in p for p in short["problems"])
    assert "ONE pass" in short["hint"]

    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    with_semi = precheck_prose(store, target, chapter=1)
    assert with_semi["verdict"] == "fail"  # 仍短于字数带
    assert with_semi["style_ok"]
    assert with_semi["style_fails"] == []

    target.write_text("值得注意的是，主角拒收改期；凭据仍被扣。", encoding="utf-8")
    meta_narration = precheck_prose(store, target, chapter=1)
    assert not meta_narration["style_ok"]
    assert any("元叙述" in str(f.get("metric")) for f in meta_narration["style_fails"])

    target.write_text(PROSE, encoding="utf-8")
    cfg = store.load_config()
    cfg["word_band"] = {"min": 10, "max": 5000}
    store.save_config(cfg)
    clean = precheck_prose(store, target, chapter=1)
    assert clean["verdict"] == "pass" and not clean["problems"]


def test_precheck_missing_file_raises(tmp_path: Path):
    proj = _project(tmp_path)
    with pytest.raises(LedgerError) as exc:
        precheck_prose(BookStore(proj), tmp_path / "nope.txt")
    assert exc.value.code == "missing_prose"


def test_draft_and_polish_actions_carry_word_band_gap(tmp_path: Path):
    proj = _project(tmp_path, word_min=2500)
    store = BookStore(proj)

    draft = chapter_next(store)
    assert draft["action"] == "draft"
    assert draft["word_band"] == {"min": 2500, "max": 5000}
    assert draft["aim_chars"] == 3200
    assert "chapter draft-submit" in draft["hint"]
    # 场次预算：把"目标 3200 字"折算成"几场 × 每场多少字"，与唯一被允许的补救动作
    # （整场补戏）用同一尺度。现场事故：第 3 章草稿 1989 字 < 2500，白写一轮全文；
    # aim 缓冲 300→700：初稿欠写 15–25%，aim=min+300 时几乎每章返工。
    assert draft["scene_budget"] == {
        "target_chars": 3200,
        "scenes": 5,
        "chars_per_scene": 700,
        "floor_chars": 2500,
    }
    assert "scene(s) of ~700 hanzi" in draft["hint"]
    # 同一口径还必须活在执笔真正读的那份文件里：`writing_brief`。
    # 预算只写在 `chapter next` 的行动载荷里时，执笔编辑读的是
    # pack 的 writing_brief，于是第 5/6/8 章（3/5）首轮草稿只有 1789–2309 汉字，
    # 全被 precheck 打回再补整场戏。
    brief = read_json(store.draft_pack_path(1))["writing_brief"]
    assert "目标长度为3200个中文字" in brief
    assert "共 5 场戏，每场约 700 汉字" in brief
    assert "写不足2500会被 `chapter draft-submit` 当场拒收" in brief
    # 提交命令的形状要教对：*-submit 不吃路径参数（5/5 个 worker 曾写成 --file 而 invalid_args）
    assert "takes NO path argument" in draft["hint"]

    # 短草稿在 draft-submit 就地拦截（历史上要等 submit 才回整链）
    Path(draft["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    rejected = stage_draft_submit(store)
    assert rejected["verdict"] == "draft_rejected"
    assert rejected["code"] == "word_count_low"
    assert rejected["chars_to_min"] == 2500 - rejected["chars"]
    assert "ONE pass" in rejected["hint"]
    assert store.read_head()["phase"] == "await_draft"
    assert store.draft_text_path(1).read_text(encoding="utf-8") == PROSE

    long_text = FILLER_SCENE * 110
    assert chinese_word_count(long_text) >= 2500
    Path(draft["draft_output_path"]).write_text(long_text, encoding="utf-8")
    assert stage_draft_submit(store)["verdict"] == "draft_accepted"

    polish = chapter_next(store)
    assert polish["action"] == "polish"
    assert polish["draft_chars"] == chinese_word_count(long_text)
    assert polish["chars_to_min"] == 0
    assert polish["aim_chars"] == 3200
    assert "word band 2500-5000 counts hanzi only" in polish["hint"]
    # 达标时不报"还差几场"，也不该出现补戏提示
    assert "gap_chars" not in polish["scene_budget"]
    assert "scenes_to_floor" not in polish["scene_budget"]
    assert "would clear the floor" not in polish["hint"]

    # 缺口→场次的折算口径直接测函数：正常路径下 draft-submit 会先拦掉短草稿，
    # 所以 polish 阶段看到的多是达标稿，"还差几场"只能在这里锁死。
    from novel_ledger_core.control.pipeline import _scene_budget

    assert _scene_budget(2800, floor=2500, gap_chars=511)["scenes_to_floor"] == 1
    assert _scene_budget(2800, floor=2500, gap_chars=1400)["scenes_to_floor"] == 2


def test_word_gate_rejects_short_polished_with_file_kept(tmp_path: Path):
    proj = _project(tmp_path, word_min=2500)
    store = BookStore(proj)
    long_text = FILLER_SCENE * 110

    draft = chapter_next(store)
    Path(draft["draft_output_path"]).write_text(long_text, encoding="utf-8")
    assert stage_draft_submit(store)["verdict"] == "draft_accepted"

    polish = chapter_next(store)
    Path(polish["polished_output_path"]).write_text(PROSE, encoding="utf-8")
    rejected = stage_polish_submit(store)
    assert rejected["verdict"] == "polish_rejected"
    assert rejected["code"] == "word_count_low"
    assert rejected["polished_kept"] is True
    assert store.polished_text_path(1).read_text(encoding="utf-8") == PROSE
    assert store.read_head()["phase"] == "await_polish"

    Path(polish["polished_output_path"]).write_text(long_text, encoding="utf-8")
    assert stage_polish_submit(store)["verdict"] == "polish_accepted"


def test_submit_checks_and_accepts_without_ready_receipt(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    advance_to_assembly(store, prose=PROSE)
    cfg = store.load_config()
    cfg["submit_check_guard"] = True
    store.save_config(cfg)

    output = _output(store, PROSE)
    accepted = submit_output(store, output)
    assert accepted["verdict"] == "accepted"
    assert store.read_head()["phase"] == "submitted"
    assert not store.submit_check_receipt_path(1).exists()


def test_submit_assembly_error_stays_in_place_without_spending_rewrite(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    advance_to_assembly(store, prose=PROSE)
    cfg = store.load_config()
    cfg["submit_check_guard"] = True
    store.save_config(cfg)

    bad = _output(store, PROSE)
    bad["l1_summary"] = ""
    rejected = submit_output(store, bad)
    assert rejected["verdict"] == "fix_assembly"
    assert store.read_head()["phase"] == "await_assembly"
    assert store.read_head()["rewrite_count"] == 0
    assert store.polished_text_path(1).exists()
    good = _output(store, PROSE)
    assert submit_output(store, good)["verdict"] == "accepted"
    assert store.read_head()["rewrite_count"] == 0


def test_style_metrics_failure_keeps_polished_file(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)

    draft = chapter_next(store)
    Path(draft["draft_output_path"]).write_text(PROSE, encoding="utf-8")
    stage_draft_submit(store)
    polish = chapter_next(store)
    bad = PROSE + "掌柜把凭据扣下。值得注意的是，主顾都看着。"
    Path(polish["polished_output_path"]).write_text(bad, encoding="utf-8")

    failed = stage_polish_submit(store)
    assert failed["verdict"] == "style_metrics_failed"
    assert failed["polished_kept"] is True
    assert store.polished_text_path(1).exists()
    assert store.polished_text_path(1).read_text(encoding="utf-8") == bad
    again = chapter_next(store)
    assert again["action"] == "polish"
    assert again["style_metrics_path"] is not None
    assert "already holds your previous attempt" in again["hint"]


def test_worker_agent_execution_mode(tmp_path: Path):
    """execution_mode=worker-agent：每章一个一次性 worker 子 agent，派发简报只有路径。"""
    proj = _project(tmp_path)
    store = BookStore(proj)

    inline = chapter_next(store)
    assert inline["execution"]["mode"] == "inline"
    assert inline["execution"]["spawn_allowed"] is False
    assert "worker" not in inline

    cfg = store.load_config()
    cfg["execution_mode"] = "worker-agent"
    store.save_config(cfg)
    worker = chapter_next(store)
    assert worker["execution"]["mode"] == "worker-agent"
    assert worker["execution"]["spawn_allowed"] is True
    brief = worker["worker"]
    assert brief["project"] == str(store.project)
    assert brief["resume_from"]["action"] == "draft"
    assert brief["stop_on"] == ["blocked", "usage_guard"]
    assert Path(brief["cli"]).exists()
    for card in brief["role_cards"].values():
        assert (Path(brief["role_cards_dir"]) / card).exists()
    # 固定文案落盘：简报只带指针，循环/卫生纪律住在协议卡里
    # （简报随每个 action 信封重发，固定文案不跟着重发——曾把一句语义 ×4 放大顶破成本门禁）。
    assert brief["protocol"] == "chapter"
    card_path = Path(brief["protocol_card"])
    assert card_path.exists()
    assert card_path == Path(brief["role_cards_dir"]) / "worker-protocol.md"
    rules = card_path.read_text(encoding="utf-8")
    assert "paths only" in rules and "status card" in rules
    assert "batch CLI calls" in rules and "--terse" in rules
    assert "thinking discipline" in rules
    assert "ROUND BUDGET" in rules and "<=12 model rounds" in rules
    # 简报本身必须保持指针粒度：不得内联 pack/正文内容，也不得再内联循环/卫生固定文案
    assert "prose" not in brief and "pack" not in brief
    assert "loop" not in brief and "hygiene" not in brief
    # skill 根要显式给出：项目内常见 `.dsh/skills/...` Junction 与解析后的真实路径不一致，
    # 没有这一行，每个新会话都要重新自证"是不是新旧两个实例混跑"（现场发生过三次）。
    assert brief["skill_root"] == str(Path(brief["cli"]).resolve().parents[1])


def test_dispatched_worker_session_never_gets_a_spawn_envelope(tmp_path: Path, monkeypatch):
    """supervisor 派发的 worker 会话里，信封必须降级为 inline，不能再附 worker 简报。

    现场曾出现：项目 config.execution_mode=worker-agent，`chapter next` 于是回
    `spawn_allowed=true` + "spawn one disposable worker subagent"，而 supervisor 的派发
    简报同一时刻写着"禁止创建、派发或 fork 任何物理子 agent"。两条指令字面互斥，
    那一章靠模型自己选择服从 job 指令才没多开一个上下文。
    判据完全走机器事实：active-job fence 的 job_id 与 NOVEL_LEDGER_JOB_ID 相等。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["execution_mode"] = "worker-agent"
    store.save_config(cfg)

    # 1) 没有 fence：普通交互会话，维持 worker-agent 派发形态。
    free = chapter_next(store)
    assert free["execution"]["mode"] == "worker-agent"
    assert free["execution"]["spawn_allowed"] is True
    assert "worker" in free

    # 2) fence 在，但本会话没有凭据：不是那个 worker，仍是交互语境（写命令另有 fence 拦）。
    store.autopilot_active_job_path.parent.mkdir(parents=True, exist_ok=True)
    store.autopilot_active_job_path.write_text(
        json.dumps({"job_id": "job-0003"}), encoding="utf-8"
    )
    monkeypatch.delenv("NOVEL_LEDGER_JOB_ID", raising=False)
    foreign = chapter_next(store)
    assert foreign["execution"]["mode"] == "worker-agent"
    assert "dispatched_job" not in foreign["execution"]

    # 3) 凭据对得上：本会话就是那个 worker —— 必须 inline，且不得再附 worker 简报。
    monkeypatch.setenv("NOVEL_LEDGER_JOB_ID", "job-0003")
    me = chapter_next(store)
    assert me["execution"]["mode"] == "inline"
    assert me["execution"]["spawn_allowed"] is False
    assert me["execution"]["dispatched_job"] is True
    assert me["execution"]["configured_mode"] == "worker-agent"
    assert me["execution"]["mode_reason"] == "supervisor_worker_session"
    assert "worker" not in me

    # 4) 项目本来就是 inline 时不误报降级：mode_reason 区分"本来就 inline"与"被语境降级"。
    cfg["execution_mode"] = "inline"
    store.save_config(cfg)
    native = chapter_next(store)
    assert native["execution"]["mode"] == "inline"
    assert native["execution"]["mode_reason"] == "supervisor_worker_session_confirmed"


def test_extend_plan_worker_brief_is_single_shot(tmp_path: Path):
    """extend worker 是章间单发任务：跑完 plan extend 即退，不碰 chapter next。"""
    from novel_ledger_core.control.pipeline import _worker_brief

    proj = _project(tmp_path)
    store = BookStore(proj)
    brief = _worker_brief(
        store, {"action": "extend_plan", "phase": "idle", "suggest_from": 16}
    )
    assert "THIS plan extension only" in brief["spawn"]
    assert brief["protocol"] == "plan"
    # 单发纪律的固定文案同样住在协议卡（简报只带指针）
    rules = Path(brief["protocol_card"]).read_text(encoding="utf-8")
    assert "exit IMMEDIATELY" in rules
    assert "do NOT run `chapter next`" in rules
    assert "chapter boundary" in rules
    assert brief["role_cards"]["creative"] == "creative-editor.md"
    assert brief["suggest_from"] == 16


def test_cli_terse_strips_orchestration_envelopes(tmp_path: Path, capsys):
    """--terse 剥掉 worker 简报与 execution 信封：worker 会话每次调用的 stdout 不再重复编排字段。"""
    from novel_ledger_core.control.cli import main as cli_main

    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["execution_mode"] = "worker-agent"
    store.save_config(cfg)

    assert cli_main(["chapter", "next", "--project", str(proj)]) == 0
    full = json.loads(capsys.readouterr().out)
    assert "worker" in full and "execution" in full

    assert cli_main(["--terse", "chapter", "next", "--project", str(proj)]) == 0
    terse = json.loads(capsys.readouterr().out)
    assert terse["action"] == "draft"  # 业务字段原样保留
    assert "worker" not in terse and "execution" not in terse
    assert "context_isolation_required" not in terse


def test_ack_session_mode_inline_compact(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    advance_to_assembly(store, prose=PROSE)
    assert submit_output(store, _output(store, PROSE))["verdict"] == "accepted"
    chapter_next(store)  # -> ack action

    strict = ack_read(store, quotes=_quotes(PROSE))
    assert strict["requires_new_session"] is True
    assert strict["session_boundary"] == "required"
    assert store.read_head()["phase"] == "idle"

    proj2 = _project(tmp_path / "inline")
    store2 = BookStore(proj2)
    advance_to_assembly(store2, prose=PROSE)
    cfg = store2.load_config()
    cfg["session_mode"] = "inline-compact"
    store2.save_config(cfg)
    accepted = submit_output(store2, _output(store2, PROSE))
    assert accepted["verdict"] == "accepted"
    chapter_next(store2)
    inline = ack_read(store2, quotes=_quotes(PROSE))
    assert inline["requires_new_session"] is False
    assert inline["session_boundary"] == "advisory"
    assert inline["session_mode"] == "inline-compact"
    assert "compacting context" in inline["hint"]


def test_terse_accepted_in_trailing_position(tmp_path: Path, capsys):
    """--terse 置于子命令之后同样生效（worker 习惯把 flag 追加在命令尾，
    顶层注册导致尾置 unrecognized → invalid_args，整条 && 链断裂）。"""
    from novel_ledger_core.control.cli import main as cli_main

    proj = _project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["execution_mode"] = "worker-agent"
    store.save_config(cfg)

    assert cli_main(["chapter", "next", "--project", str(proj), "--terse"]) == 0
    terse = json.loads(capsys.readouterr().out)
    assert terse["ok"] is True
    assert "worker" not in terse and "execution" not in terse
