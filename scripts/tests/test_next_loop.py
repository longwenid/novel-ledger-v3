from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.decoupled_helpers import advance_to_assembly, decoupled_submit, make_chapter, make_plan, write_plan
from novel_ledger_core.control.cli import main
from novel_ledger_core.content.gates import WRITE_KEYS
from novel_ledger_core.content.pack import assemble_pack
from novel_ledger_core.control.pipeline import (
    ack_read,
    chapter_next,
    record_usage,
    retry_authorize,
    status,
    submit_output,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, chinese_word_count, read_json


FILLER = "夜色沉沉刀光起风过竹林人影动旧怨新仇一线牵潮退石出船还在"


def _cn(n: int) -> str:
    return (FILLER * ((n // len(FILLER)) + 1))[:n]


def _plan() -> dict:
    return make_plan(
        [
            make_chapter(
                1,
                tags=("市集", "凭据"),
                beats=[
                    {"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"},
                    {"id": "b2", "required": True, "text": "凭据仍被扣", "must": "凭据"},
                ],
            ),
            make_chapter(
                2,
                location="账房",
                tags=("账房",),
                beats=[{"id": "b3", "required": True, "text": "主角夜里再去翻账", "must": "翻账"}],
            ),
        ]
    )


def _kb() -> dict:
    return {
        "cards": [
            {
                "id": "dock",
                "kind": "geography",
                "title": "市集",
                "body": "涨潮时木桩没过膝。夜里只有值更的人提灯。凭据房在柜台尽头。",
                "tags": ["市集", "凭据"],
            },
            {
                "id": "far-rule",
                "kind": "world",
                "title": "远方气候",
                "body": "北原冬天封河，与市集无关的长篇设定，用来证明切片不会整库灌入。",
                "tags": ["北原", "气候"],
            },
        ]
    }


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    plan_path = write_plan(tmp_path, _plan())
    kb_path = tmp_path / "kb.json"
    kb_path.write_text(json.dumps(_kb(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(
        proj,
        plan_path=plan_path,
        kb_path=kb_path,
        protagonist="主角",
        word_min=20,
        word_max=5000,
    )
    _disable_low_water(BookStore(proj))
    return proj


def _good_output(store: BookStore, *, chapter: int = 1) -> dict:
    pack = read_json(store.current_pack_path)
    if chapter == 1:
        prose = "主角站在市集，对着掌柜说拒收改期。凭据还压在柜台那头。" + _cn(40)
        hits = ["b1", "b2"]
        named = ["主角", "掌柜"]
        moves = [{"who": "主角", "to": "市集"}]
        summary = "主角拒收改期，凭据仍被扣。"
    else:
        prose = "夜里主角摸进账房翻账，掌柜在门口抽烟不拦。" + _cn(40)
        hits = ["b3"]
        named = ["主角", "掌柜"]
        moves = [{"who": "主角", "to": "账房"}]
        summary = "主角夜里翻账。"
    assert chinese_word_count(prose) >= 20
    return {
        "prose": prose,
        "l1_summary": summary,
        "state_delta": {
            "moves": moves,
            "facts": [{"who": "主角", "text": summary}],
            "debts": [],
            "named": named,
            "new_names": [],
        },
        "memory": {"voice_concepts": []},
        "pack_hash": pack["pack_hash"],
        "beats_hit": hits,
    }


def _disable_low_water(store: BookStore) -> None:
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    store.save_config(cfg)


def _ack_quotes(prose: str, *snippets: str) -> list[str]:
    quotes = [s for s in snippets if len(s.strip()) >= 6]
    if len(quotes) >= 3:
        return quotes[:3]
    for chunk in (
        "主角站在市集",
        "凭据还压在柜台那头",
        "对着掌柜说拒收改期",
        "夜里主角摸进账房翻账",
        "掌柜在门口抽烟不拦",
    ):
        if chunk in prose and chunk not in quotes:
            quotes.append(chunk)
        if len(quotes) >= 3:
            break
    return quotes


def _bad_output(store: BookStore) -> dict:
    pack = read_json(store.current_pack_path)
    return {
        "prose": "他走了一圈。" + _cn(30),
        "l1_summary": "空转。",
        "state_delta": {"moves": [], "facts": [], "debts": [], "named": ["主角"], "new_names": []},
        "memory": {},
        "pack_hash": pack["pack_hash"],
        "beats_hit": [],
    }


def test_next_happy_path_one_chapter(project: Path):
    store = BookStore(project)
    r1 = chapter_next(store)
    assert r1["ok"] is True
    assert r1["action"] == "draft"
    assert r1["chapter"] == 1
    assert r1["execution"]["mode"] == "inline"
    assert r1["execution"]["spawn_allowed"] is False
    pack = read_json(store.current_pack_path)
    assert "pack_hash" not in r1  # 哈希只在 assemble 视图给
    assert pack["now_card"]["name"] == "主角"
    assert pack["voice_concepts"]
    # init 默认蒸馏 shijing：不跑 voice apply 也有文风，首位保留附件不可移植边界。
    assert "移植" in pack["voice_concepts"][0]
    assert "限制视角" in pack["voice_writing_text"]
    assert "voice_manual_text" not in pack
    assert "voice_writing_text" in pack["instruction"]
    kb_ids = {c["id"] for c in pack["kb_slice"]}
    assert "dock" in kb_ids
    assert "far-rule" not in kb_ids
    assert len(pack["kb_slice"]) < 2 or "far-rule" not in kb_ids

    submitted = decoupled_submit(store, _good_output(store))
    assert submitted["ok"] is True
    assert submitted["verdict"] == "accepted"

    r2 = chapter_next(store)
    assert r2["ok"] is True
    assert r2["action"] == "ack"
    assert r2["chapter"] == 1
    assert store.chapter_md_path(1).exists()
    assert r2["prose_hash"]

    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    ack = ack_read(
        store,
        quotes=_ack_quotes(prose, "主角站在市集", "凭据还压在柜台那头", "对着掌柜说拒收改期"),
        verdict="pass",
    )
    assert ack["ok"] is True
    assert ack["verdict"] == "acked"
    assert ack["last_acked_ch"] == 1
    assert ack["stop"] is True
    assert ack["session_boundary"] == "required"
    assert ack["requires_new_session"] is True
    assert "主角站在市集" in prose

    r3 = chapter_next(store)
    assert r3["ok"] is True
    assert r3["action"] == "draft"
    assert r3["chapter"] == 2


def test_output_paths_grouped_by_volume(tmp_path: Path):
    """正文/摘要/ack 按卷落盘：chapters/vol-XXXX、summaries/l1/vol-XXXX、acks/vol-XXXX。"""
    plan = _plan()
    plan["chapters"][0]["volume"] = 1
    plan["chapters"][1]["volume"] = "第二卷"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "vols"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    assert store.chapter_md_path(1).parent.name == "vol-0001"
    assert store.chapter_md_path(2).parent.name == "vol-0002"
    assert store.summary_path(1).parent.name == "vol-0001"
    assert store.ack_path(2).parent.name == "vol-0002"


def test_volume_label_parses_chinese_numerals_with_zeros():
    """中文卷号要能解析零位与十/百/千/万。

    历史缺陷：`第一百零三卷` 因为不含在字符类里，静默掉进"文本卷名"分支，
    同一本书的目录命名会混用 `vol-0103` 与 `vol-第一百零三卷` 两套风格。
    """
    from novel_ledger_core.infra.store import _volume_label

    assert _volume_label("第一百零三卷") == "vol-0103"
    assert _volume_label("第一百二十三卷") == "vol-0123"
    assert _volume_label("第一千零五卷") == "vol-1005"
    assert _volume_label("第二十三卷") == "vol-0023"
    assert _volume_label("第十卷") == "vol-0010"
    # 非编号文本仍走清洗分支，不受影响
    assert _volume_label("番外卷") == "vol-番外卷"


def test_ack_voice_note_goes_to_session_notes_only(project: Path):
    """真走一遍 ack 路径：`--voice-note` 与写者回灌的 `memory.voice_concepts`
    都只进 session_notes 窗口，蒸馏的长驻 concepts 一个字不动。"""
    store = BookStore(project)
    chapter_next(store)
    output = _good_output(store)
    output["memory"] = {"voice_concepts": ["写者回灌：先端下来再说"]}
    decoupled_submit(store, output)
    chapter_next(store)  # commit
    before = store.load_voice_concepts()
    assert before  # init 自动蒸馏写入的文风概念
    assert store.load_voice_session_notes() == ["写者回灌：先端下来再说"]

    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    ack = ack_read(
        store,
        quotes=_ack_quotes(prose, "主角站在市集", "凭据还压在柜台那头", "对着掌柜说拒收改期"),
        verdict="pass",
        voice_note="灶台上的水开了就先端下来",
    )
    assert ack["verdict"] == "acked"
    assert store.load_voice_concepts() == before
    assert store.load_voice_session_notes() == [
        "写者回灌：先端下来再说",
        "灶台上的水开了就先端下来",
    ]
    # 两个窗口都能进 pack（文风概念 21 条，cap 24，有3槽余量给笔记）
    concepts = assemble_pack(store, 2)["voice_concepts"]
    # 21条蒸馏概念 + 2条笔记 = 23条，应该都能进入（cap=24）
    assert len(concepts) <= 24
    assert "灶台上的水开了就先端下来" in concepts  # 笔记应该进入了


def test_negative_voice_note_is_reported_not_swallowed(project: Path):
    """以「不要/禁止」开头的 voice-note 会被过滤，但必须回传，不能静默吞掉作者指令。

    历史缺陷：`_append_voice_concept` 直接 return，ack 照常返回 ok——
    作者以为"我说过了"，实际这条指令没进任何通道（既不是红线也不是正典）。
    """
    store = BookStore(project)
    chapter_next(store)
    decoupled_submit(store, _good_output(store))
    chapter_next(store)  # commit
    before = store.load_voice_session_notes()

    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    ack = ack_read(
        store,
        quotes=_ack_quotes(prose, "主角站在市集", "凭据还压在柜台那头", "对着掌柜说拒收改期"),
        verdict="pass",
        voice_note="不要写这种顺口对白",
    )
    assert ack["verdict"] == "acked"
    assert ack["voice_note_dropped"]["reason"].startswith("note starts")
    assert store.load_voice_session_notes() == before


def test_next_reports_the_state_it_advanced(project: Path):
    """`chapter next` 不是只读命令：它推进 HEAD 时必须在输出里说清推进了什么。

    现场事故：有人只是"想看看下一章是什么"就跑了一次 `next`，HEAD 从 `chapter=3/phase=idle`
    变成 `chapter=4/phase=await_draft`、quality.jsonl 多一条 `chapter 4 begin`——输出里却
    没有任何一处提示状态已被本命令改动，事后审计很容易读成"第 4 章已经开工"。
    只读台面用 `status`；`next` 至少要把差异回报出来。
    """
    store = BookStore(project)

    # idle → await_draft：这一步真的落盘推进了状态
    opened = chapter_next(store)
    assert opened["action"] == "draft"
    transition = opened["head_transition"]
    assert transition["changed"]["chapter"] == [0, 1]
    assert transition["changed"]["phase"] == ["idle", "await_draft"]
    assert "status" in transition["note"]

    # 同一阶段内重复调用不再推进：不得平白多出这条字段
    repeated = chapter_next(store)
    assert repeated["action"] == "draft"
    assert "head_transition" not in repeated


def test_cannot_begin_next_without_ack(project: Path):
    store = BookStore(project)
    chapter_next(store)
    decoupled_submit(store, _good_output(store))
    committed = chapter_next(store)
    assert committed["action"] == "ack"
    again = chapter_next(store)
    assert again["ok"] is True
    assert again["action"] == "ack"
    assert again["chapter"] == 1
    head = store.read_head()
    assert head["phase"] == "await_ack"
    assert head["last_committed_ch"] == 1
    assert head["last_acked_ch"] == 0
    st = status(store)
    assert st["last_acked_ch"] == 0
    assert st["runtime"]["version"] == "3.0.0"
    assert st["runtime"]["control_fingerprint"].startswith("sha256:")
    assert st["runtime"]["skill_root"].endswith("v3")


def test_rewrite_once_then_commit(project: Path):
    store = BookStore(project)
    chapter_next(store)
    pack_hash = read_json(store.current_pack_path)["pack_hash"]
    bad = decoupled_submit(store, _bad_output(store))
    assert bad["ok"] is True
    assert bad["verdict"] == "rewrite"
    assert bad["phase"] == "await_draft"  # 正文类失败回正文草稿
    again = chapter_next(store)
    assert again["action"] == "draft"
    assert read_json(store.current_pack_path)["pack_hash"] == pack_hash
    good = decoupled_submit(store, _good_output(store))
    assert good["verdict"] == "accepted"
    committed = chapter_next(store)
    assert committed["action"] == "ack"
    assert committed["chapter"] == 1
    assert store.read_head()["phase"] == "await_ack"


def test_second_gate_fail_blocks(project: Path):
    store = BookStore(project)
    chapter_next(store)
    first = decoupled_submit(store, _bad_output(store))
    assert first["verdict"] == "rewrite"
    second = decoupled_submit(store, _bad_output(store))
    assert second["ok"] is True
    assert second["verdict"] == "blocked"
    assert second["stop"] is True
    nxt = chapter_next(store)
    assert nxt["action"] == "blocked"
    assert nxt["stop"] is True
    assert store.read_head()["phase"] == "blocked"


def test_ack_rejects_token_sized_quote(project: Path):
    store = BookStore(project)
    chapter_next(store)
    decoupled_submit(store, _good_output(store))
    chapter_next(store)
    with pytest.raises(LedgerError) as exc:
        ack_read(store, quotes=["的", "的", "的"], verdict="pass")
    assert exc.value.code == "ack_quote_too_short"
    head = store.read_head()
    assert head["phase"] == "await_ack"
    assert head["last_acked_ch"] == 0


def test_next_extends_when_plan_is_exhausted_before_book_target(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    kb_path = tmp_path / "kb.json"
    one = _plan()
    one["chapters"] = one["chapters"][:1]
    plan_path.write_text(json.dumps(one, ensure_ascii=False), encoding="utf-8")
    kb_path.write_text(json.dumps(_kb(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "onech"
    proj.mkdir()
    init_project(
        proj,
        plan_path=plan_path,
        kb_path=kb_path,
        protagonist="主角",
        word_min=20,
        word_max=5000,
    )
    store = BookStore(proj)
    _disable_low_water(store)
    chapter_next(store)
    decoupled_submit(store, _good_output(store))
    chapter_next(store)
    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    ack_read(store, quotes=_ack_quotes(prose, "主角站在市集"), verdict="pass")
    done = chapter_next(store)
    assert done["ok"] is True
    assert done["action"] == "extend_plan"
    assert done["stop"] is False
    assert done["reason"] == "plan_exhausted_before_book_target"
    head = store.read_head()
    assert head["phase"] == "idle"
    assert head["last_committed_ch"] == 1
    assert head["last_acked_ch"] == 1
    assert head["chapter"] == 1
    again = chapter_next(store)
    assert again["action"] == "extend_plan"
    assert again["stop"] is False


def test_kb_slice_opening_pack_skips_rule_manual_until_named(tmp_path: Path):
    """装配层：开篇拍点不装规则说明书；拍点点名后才装。NOW 卡仍在。"""
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：主角要拿回被扣的凭据。",
        "chapters": [
            {
                "chapter": 1,
                "location": "边境小城",
                "present": ["主角", "眼前人"],
                "tags": ["开场", "边境小城"],
                "beats": [
                    {"id": "b1", "required": True, "text": "压力先落到身上，来不及解释这个世界", "must": "压力"},
                    {"id": "b2", "required": True, "text": "眼前有人要他当场应对", "must": "眼前"},
                ],
            },
            {
                "chapter": 2,
                "location": "集市",
                "present": ["主角"],
                "tags": [],
                "beats": [
                    {"id": "b1", "required": True, "text": "主角看见环形光", "must": "环形光"},
                ],
            },
        ],
    }
    kb = {
        "cards": [
            {
                "id": "总纲--硬-总因",
                "kind": "world",
                "title": "【硬】总因",
                "tags": ["总纲"],
                "body": "其本质是规则的指数级跃迁。",
                "always": True,
            },
            {
                "id": "总纲--硬-一-宏观异象-天象异变",
                "kind": "world",
                "title": "宏观异象",
                "tags": ["总纲"],
                "body": "高阶存在边缘泛着环形光晕。",
            },
        ]
    }
    plan_path = tmp_path / "plan.json"
    kb_path = tmp_path / "kb.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    kb_path.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "preset"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, kb_path=kb_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    pack1 = assemble_pack(store, 1)
    assert pack1["now_card"]["name"] == "主角"
    blob1 = json.dumps(pack1["kb_slice"], ensure_ascii=False)
    assert "环形光" not in blob1
    assert "总因" not in blob1

    pack2 = assemble_pack(store, 2)
    blob2 = json.dumps(pack2["kb_slice"], ensure_ascii=False)
    assert "环形光" in blob2
    assert pack2["now_card"]["name"] == "主角"


def test_submit_without_telemetry_does_not_fabricate_zero_usage(project: Path):
    store = BookStore(project)
    chapter_next(store)
    submitted = decoupled_submit(store, _good_output(store))
    assert submitted["verdict"] == "accepted"
    assert not store.usage_path.exists()
    assert status(store)["usage"]["telemetry"] == "missing"


def test_cli_missing_project_is_json(capsys):
    code = main(["status"])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error"]["code"] in {"missing_project", "invalid_args"}


def test_cli_project_flag_works_in_both_positions(project: Path, capsys):
    """前置 `--project X status` 曾被子解析器的 None 默认冲回 missing_project。

    --terse 用子解析器 SUPPRESS
    修过同款坑，--project 漏了同步。两种位置都必须可用；尾置仍以尾置值为准。
    """
    code = main(["--project", str(project), "status"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True

    code = main(["status", "--project", str(project)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True


def test_retry_authorize_rewrites_last_acked_from_idle(project: Path):
    """已 ack 的最后一章：idle 下 retry-authorize 回滚账本、删旧稿、装配新 pack，不跳到第 2 章。

    整本从第 1 章重来时还要清掉废稿 session_notes，否则「用鼻血写…」会再进 pack。
    """
    store = BookStore(project)
    chapter_next(store)
    output = _good_output(store)
    output["memory"] = {"voice_concepts": ["用鼻血写靠近高境"]}
    decoupled_submit(store, output)
    chapter_next(store)
    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    ack = ack_read(
        store,
        quotes=_ack_quotes(prose, "主角站在市集", "凭据还压在柜台那头", "对着掌柜说拒收改期"),
        verdict="pass",
    )
    assert ack["verdict"] == "acked"
    assert "用鼻血写靠近高境" in store.load_voice_session_notes()
    head = store.read_head()
    assert head["phase"] == "idle"
    assert head["last_acked_ch"] == 1
    assert head["last_committed_ch"] == 1

    auth = retry_authorize(store, actor="human", reason="rewrite last acked chapter")
    assert auth["ok"] is True
    assert auth["verdict"] == "authorized"
    assert auth["chapter"] == 1
    head = store.read_head()
    assert head["phase"] == "await_draft"
    assert head["chapter"] == 1
    assert head["last_committed_ch"] == 0
    assert head["last_acked_ch"] == 0
    assert not store.chapter_md_path(1).exists()
    assert not store.ack_path(1).exists()
    assert store.load_voice_session_notes() == []
    nxt = chapter_next(store)
    assert nxt["action"] == "draft"
    assert nxt["chapter"] == 1
    pack = read_json(store.current_pack_path)
    assert pack["chapter"] == 1
    assert pack["voice_writing_text"]
    assert "pack_hash" not in nxt
    assert "用鼻血写靠近高境" not in pack["voice_concepts"]


def test_ledger_conflict_blocks_and_retry_authorize(project: Path):
    store = BookStore(project)
    snap = read_json(store.snapshot_path)
    snap["entities"]["掌柜"] = {"id": "掌柜", "dead": True, "facts": [], "location": "市集"}
    atomic_json(store.snapshot_path, snap)
    chapter_next(store)
    submitted = decoupled_submit(store, _good_output(store))
    assert submitted["verdict"] == "accepted"
    nxt = chapter_next(store)
    assert nxt["ok"] is True
    assert nxt["action"] == "blocked"
    assert nxt["stop"] is True
    head = store.read_head()
    assert head["phase"] == "blocked"
    assert head["blocked"]["reason"] == "ledger_conflict"
    auth = retry_authorize(store, actor="human", reason="fix delta")
    assert auth["ok"] is True
    assert store.read_head()["phase"] == "await_draft"


def test_empty_plan_next_is_not_complete(tmp_path: Path):
    proj = tmp_path / "empty"
    proj.mkdir()
    init_project(proj, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    assert store.load_plan()["chapters"] == []
    with pytest.raises(LedgerError) as exc:
        chapter_next(store)
    assert exc.value.code == "empty_plan"
    head = store.read_head()
    assert head["phase"] == "idle"
    assert head["last_committed_ch"] == 0
    assert head["last_acked_ch"] == 0


def test_ack_requires_minimum_quotes(project: Path):
    store = BookStore(project)
    chapter_next(store)
    decoupled_submit(store, _good_output(store))
    chapter_next(store)
    with pytest.raises(LedgerError) as exc:
        ack_read(store, quotes=["主角站在市集", "拒收改期"], verdict="pass")
    assert exc.value.code == "ack_insufficient_quotes"
    head = store.read_head()
    assert head["phase"] == "await_ack"


def test_plan_low_water_extend_plan(tmp_path: Path):
    plan = _plan()
    plan["chapters"] = [
        {
            "chapter": i,
            "location": "市集",
            "present": ["主角", "掌柜"],
            "tags": ["市集"],
            "beats": [{"id": "b1", "required": True, "text": f"第{i}章凭据事件", "must": "凭据"}],
        }
        for i in range(1, 6)
    ]
    plan_path = tmp_path / "plan.json"
    kb_path = tmp_path / "kb.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    kb_path.write_text(json.dumps(_kb(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "lowwater"
    proj.mkdir()
    init_project(
        proj,
        plan_path=plan_path,
        kb_path=kb_path,
        protagonist="主角",
        word_min=20,
        word_max=5000,
    )
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    store.save_config(cfg)
    # 「首次 next 直接 draft」：低水位提示只在已开写后触发，
    # 本用例预置第 1 章已提交 ack，制造库存 4 ≤ 5 的书中部场景
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)

    nudge = chapter_next(store)
    assert nudge["action"] == "extend_plan"
    assert nudge["stop"] is False
    assert nudge["remaining_in_plan"] == 4
    assert nudge["suggest_from"] == 6
    assert nudge["chapter"] == 2
    assert "一场戏" in nudge["hint"]
    assert "每章 5 个事件" in nudge["hint"]
    # 扩纲必须独立会话：hint 显式要求宿主另派一次性 plan worker，禁止并入章节 worker
    # （内联扩纲让单章 token 与时长翻倍）；档位要求随信封走。
    assert "独立会话" in nudge["hint"]
    assert "禁止并入章节 worker 会话" in nudge["hint"]
    assert nudge["execution"]["mode"] == "inline"
    assert nudge["execution"]["spawn_allowed"] is False
    assert nudge["execution"]["role"] == "planning"
    assert nudge["execution"]["context_isolation"] == "editorial_state"
    assert nudge["execution"]["preferred_tier"] == "strongest"

    write = chapter_next(store)
    assert write["action"] == "draft"
    assert write["chapter"] == 2


def test_low_water_usage_stop_blocks_the_next_prepared_model_action(tmp_path: Path):
    """idle 只重置上一章；同一候选章的规划用量不能借 idle 多放行一次请求。"""
    plan_path = tmp_path / "lowwater-budget-plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "lowwater-budget"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 5
    cfg["usage_budget"]["stop_input_per_chapter"] = 10
    store.save_config(cfg)
    # 低水位提示只在已开写后触发（首跑直接 draft），预置第 1 章已提交 ack
    head = store.read_head()
    head.update({"phase": "idle", "chapter": 1, "last_committed_ch": 1, "last_acked_ch": 1})
    store.write_head(head)

    nudge = chapter_next(store)
    assert nudge["action"] == "extend_plan"
    usage = record_usage(
        store,
        chapter=2,
        stage="extend_plan",
        request_id="req-lowwater-over-budget",
        usage={"uncached_input_tokens": 11, "output_tokens": 1},
    )
    assert usage["stop"] is True

    guarded = chapter_next(store)
    assert guarded["action"] == "usage_guard"
    assert guarded["prepared_action"] == "draft"
    assert guarded["chapter"] == 2


def test_init_rejects_plan_without_protagonist(tmp_path: Path):
    """章拍真源缺 protagonist：开书时就拒，不要等到第一章 missing_now_card。"""
    plan = _plan()
    plan["protagonist"] = ""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "noprot"
    proj.mkdir()
    with pytest.raises(LedgerError) as exc:
        init_project(proj, plan_path=plan_path)
    assert exc.value.code == "missing_protagonist"
    assert not BookStore(proj).head_path.exists()  # 失败不留半初始化项目


def test_init_protagonist_flag_backfills_plan(tmp_path: Path):
    plan = _plan()
    plan["protagonist"] = ""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "flagprot"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    assert store.load_plan()["protagonist"] == "主角"
    assert store.load_config()["protagonist"] == "主角"


def test_init_without_plan_or_protagonist_rejected(tmp_path: Path):
    proj = tmp_path / "seedonly"
    proj.mkdir()
    with pytest.raises(LedgerError) as exc:
        init_project(proj)
    assert exc.value.code == "missing_protagonist"


def test_full_plan_fields_reach_pack_and_next_writes(tmp_path: Path):
    """章拍字段齐全（plan-contract.md 的样例形状）时 chapter next 正常返回 write，
    且 location/present/goal/speech 都进了 pack —— 缺它们会静默降级，故此处逐项断言。"""
    plan = _plan()
    plan["chapters"][0]["goal"] = "把被扣的凭据要回来"
    plan["chapters"][0]["speech"] = ["这事我担着"]
    plan_path = tmp_path / "plan.json"
    kb_path = tmp_path / "kb.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    kb_path.write_text(json.dumps(_kb(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "fullplan"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, kb_path=kb_path)
    store = BookStore(proj)
    _disable_low_water(store)
    r = chapter_next(store)
    assert r["ok"] is True
    assert r["action"] == "draft"
    pack = read_json(store.current_pack_path)
    assert pack["now_card"]["name"] == "主角"
    assert pack["now_card"]["location"] == "市集"
    assert pack["now_card"]["goal"] == "把被扣的凭据要回来"
    assert pack["now_card"]["speech"] == ["这事我担着"]
    assert [c["name"] for c in pack["present_cards"]] == ["掌柜"]


def test_example_plan_template_is_valid_source_of_truth(tmp_path: Path):
    """templates/plan.chapters.example.json 必须真的能开书写第一章。"""
    from novel_ledger_core.control.bootstrap import TEMPLATES_DIR

    example = TEMPLATES_DIR / "plan.chapters.example.json"
    assert example.is_file()
    proj = tmp_path / "fromexample"
    proj.mkdir()
    init_project(proj, plan_path=example, word_min=20, word_max=5000)
    store = BookStore(proj)
    _disable_low_water(store)
    r = chapter_next(store)
    assert r["action"] == "draft"
    pack = read_json(store.current_pack_path)
    assert pack["now_card"]["name"]
    assert pack["beats"]


def test_beats_hit_is_required_write_key(project: Path):
    assert "beats_hit" in WRITE_KEYS
    store = BookStore(project)
    chapter_next(store)
    out = _good_output(store)
    del out["beats_hit"]
    result = decoupled_submit(store, out)
    assert result["ok"] is True
    assert result["verdict"] == "fix_assembly"
    codes = {v["code"] for v in result["violations"]}
    assert "missing_field" in codes
    assert any(v.get("field") == "beats_hit" for v in result["violations"])


def test_stage_submit_accepts_its_own_path_and_rejects_others(project: Path, capsys):
    """`draft-submit --file` / `polish-submit --file` 要么重复本阶段落点，要么明确报错。

    任务书曾把「先 `chapter precheck --file` 自查，再 draft-submit」
    写在一句话里，5/5 个 worker 都写成 `draft-submit --file <path>`，被 argparse 判成
    invalid_args 白损失一轮。修法不是禁止该参数，而是允许直觉写法——但只允许它指向本阶段
    自己的落点，免得有人指一个手改过的文件就"提交"了。
    """
    store = BookStore(project)
    draft = chapter_next(store)
    staged = Path(draft["draft_output_path"])
    staged.write_text("主角站在市集，对掌柜说拒收改期。凭据还压在柜台那头。", encoding="utf-8")

    def run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        code = main(list(argv))
        return code, capsys.readouterr().out

    # 1) 指一个别的文件：明确报错，不许静默采用（文件检查先于阶段推进，相位没动）
    other = Path(project) / "elsewhere.txt"
    other.write_text("不是这一阶段的产物", encoding="utf-8")
    code, out = run("chapter", "draft-submit", "--project", str(project), "--file", str(other))
    assert code != 0
    assert "wrong_staging_file" in out

    # 2) 重复本阶段落点：放行——命令真的跑到了业务层（拿到 draft 机检结论），不再是 invalid_args
    code, out = run("chapter", "draft-submit", "--project", str(project), "--file", str(staged))
    assert code == 0, out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload.get("verdict") in {"draft_accepted", "draft_rejected"}

    # 3) 不带 --file 的老写法仍然被 argparse 接受：这一阶段已经提交过，所以只应报 wrong_phase，
    #    绝不能是 invalid_args（那才是本用例要防的退化）。
    code, out = run("chapter", "draft-submit", "--project", str(project))
    assert "invalid_args" not in out
    assert "wrong_phase" in out
