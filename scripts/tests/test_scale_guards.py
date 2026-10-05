from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.decoupled_helpers import advance_to_assembly, decoupled_submit
from novel_ledger_core.ledger.ledger import apply_event, select_debts, select_hooks, select_relations
from novel_ledger_core.content.pack import assemble_pack, slice_kb
from novel_ledger_core.control.pipeline import ack_read, chapter_next, submit_output
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, atomic_json, read_json


def _plan() -> dict:
    return {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：主角要回那张被扣的凭据。",
        "chapters": [
            {
                "chapter": 1,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "tags": ["市集", "凭据"],
                "recap": "前情：主角被扣凭据，掌柜管着柜台那头。",
                "beats": [{"id": "b1", "required": True, "text": "主角拒收改期", "must": "拒收"}],
            }
        ],
    }


def test_per_volume_spine_goal_drives_now_card(tmp_path: Path):
    """plan.volumes 可按卷提供 spine/goal，未逐章写 goal 时也不再误用第一卷目标。"""
    plan = _plan()
    plan["volumes"] = {
        "1": {"spine": "第一卷：主角要回那张被扣的凭据。", "goal": "第一卷目标：拿回凭据。"},
        "第二卷": {"spine": "第二卷：主角北上追查线人。", "goal": "第二卷目标：找到线人。"},
    }
    plan["chapters"].append(
        {
            "chapter": 2,
            "volume": "第二卷",
            "location": "渡口",
            "present": ["主角"],
            "beats": [{"id": "b1", "required": True, "text": "主角登船北上", "must": "登船"}],
        }
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "vols"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)

    pack1 = assemble_pack(store, 1)
    assert pack1["now_card"]["goal"] == "第一卷目标：拿回凭据。"
    assert pack1["volume_spine"] == "第一卷：主角要回那张被扣的凭据。"

    pack2 = assemble_pack(store, 2)
    assert pack2["now_card"]["goal"] == "第二卷目标：找到线人。"
    assert pack2["volume_spine"] == "第二卷：主角北上追查线人。"


def _seed_big_snapshot(store: BookStore, n: int) -> None:
    snap = {"chapter": 0, "entities": {}, "debts": [], "occupancy": {}}
    for i in range(1, n + 1):
        who = f"人物{i:05d}"
        event = {
            "chapter": i,
            "state_delta": {
                "moves": [{"who": who, "to": f"地点{i % 20:02d}"}],
                "new_names": [who],
            },
        }
        snap = apply_event(snap, event)
    # 主角在场：掌柜也放进市集
    snap = apply_event(
        snap,
        {
            "chapter": n,
            "state_delta": {"moves": [{"who": "掌柜", "to": "市集"}], "named": ["掌柜"]},
        },
    )
    atomic_json(store.snapshot_path, snap)


def test_occupancy_in_pack_is_bounded_by_location(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    _seed_big_snapshot(store, 3000)

    pack = assemble_pack(store, 1)
    occ = pack["state_near"]["occupancy"]
    # 只含本章相关地点（市集 / 掌柜所在地），不能是全书 3000 人的占用表
    assert set(occ.keys()) <= {"市集"}
    assert len(occ.get("市集") or []) <= 12
    total_names = sum(len(v) for v in occ.values())
    assert total_names < 50
    # 3000 个命名实体绝不能被整表塞进 occupancy（recent 仍有界，此处只看 occupancy）
    occ_blob = json.dumps(occ, ensure_ascii=False)
    assert "人物03000" not in occ_blob
    assert "地点19" not in occ_blob


def test_pack_has_write_contract(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    pack = assemble_pack(BookStore(proj), 1)
    contract = pack["write_contract"]
    # prose 不再是必填 key：正文真源是阶段二终稿，脚本在 submit 时读盘注入
    for key in ("l1_summary", "state_delta", "memory", "pack_hash", "beats_hit"):
        assert key in contract["keys"]
    assert "prose" not in contract["keys"]
    assert contract["gates"]
    # canonical 契约只保留机检的硬不变量；阶段细节由 draft/polish 视图承担
    assert contract["gates"][0].startswith("只从终稿正文提取账本字段")
    joined_gates = " ".join(contract["gates"])
    assert "expected_delta" in joined_gates
    assert "chapter submit" in joined_gates
    assert any(gate.startswith("pack_hash 必须逐字回显") for gate in contract["gates"])


def test_recap_injected_when_present(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    pack = assemble_pack(BookStore(proj), 1)
    assert pack.get("recap") == "前情：主角被扣凭据，掌柜管着柜台那头。"


def test_debts_pass_through_due(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    store = BookStore(proj)
    snap = read_json(store.snapshot_path)
    snap["debts"] = [
        {"id": "d1", "who": "主角", "text": "欠掌柜一坛酒", "status": "open", "due": "第 8 章"},
        {"id": "d2", "who": "别人", "text": "无关的债", "status": "open", "location": "别处"},
    ]
    atomic_json(store.snapshot_path, snap)

    out, _omitted = select_debts(store, location="市集", present=["主角", "掌柜"], cap=8)
    assert any(d["id"] == "d1" and d.get("due") == "第 8 章" for d in out)
    assert not any(d.get("due") is None for d in out if d["id"] == "d1")
    # 未设 due 的债不出现该字段（保持 JSON 干净）
    assert all("due" in d for d in out)  # d1 有 due；d2 不相关不会入选


def test_pack_caps_have_occupancy_limits():
    from novel_ledger_core.infra.store import DEFAULT_CONFIG

    # occupancy_locations 曾是死配置（代码硬顶 3 个地点，从不读这个键）
    assert "occupancy_locations" not in DEFAULT_CONFIG["pack_caps"]
    assert DEFAULT_CONFIG["pack_caps"]["occupancy_per_location"] >= 1


def test_submit_accepts_output_without_prose(tmp_path: Path):
    """事实编辑不再回显正文：省略 prose key，脚本从终稿注入后照常 accepted。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    out = _short_output(store)
    text = out.pop("prose")
    r = decoupled_submit(store, out, prose=text)
    assert r["ok"] is True
    assert r["verdict"] == "accepted"
    # canonical output 落盘时必须被注入回正文（commit 依赖它，不信子 agent 回显）
    assert read_json(store.output_path)["prose"] == text


def _short_output(store: BookStore) -> dict:
    pack = read_json(store.current_pack_path)
    return {
        "prose": "主角在市集拒收了改期的凭据。",
        "l1_summary": "主角拒收改期。",
        "state_delta": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "拒收改期"}],
            "debts": [],
            "named": ["主角", "掌柜"],
            "new_names": [],
        },
        "memory": {"voice_concepts": []},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1"],
    }


def _make_project(tmp_path: Path) -> Path:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "bookproj"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    return proj


def test_short_chapter_accepted_with_warning_when_word_band_soft(tmp_path: Path):
    # 项目显式关闭硬门禁时：短章（beats 全中）照常接受，只报 word_count_low 软信号
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["word_band_enforce"] = False
    store.save_config(cfg)
    chapter_next(store)
    r = decoupled_submit(store, _short_output(store))
    assert r["ok"] is True
    assert r["verdict"] == "accepted"
    assert store.read_head()["phase"] == "submitted"
    codes = {w["code"] for w in r["warnings"]}
    assert "word_count_low" in codes
    low = next(w for w in r["warnings"] if w["code"] == "word_count_low")
    assert "补拍" in low["hint"]  # 指向章拍事件不够，而不是注水
    # 每章 5 场戏是编拍纪律（带收成 5–5）：1 条 beat 只要 must 命中，submit 不得 rejected
    viol = {v["code"] for v in r.get("violations") or []}
    assert "beats_count_off_band" not in viol
    assert not any("beat" in c and "count" in c for c in viol)


def test_short_chapter_rewrites_when_word_band_enforced(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    # staging 的 draft/polish 字数闸与 submit 闸共用 word_band_enforce：
    # 阶段推进时先关闸（否则在 draft-submit 就被 draft_rejected 拦下），
    # 润色收口后再开闸，验证 submit 层的 word_count_low 防线仍然独立成立。
    chapter_next(store)
    out = _short_output(store)
    text = out.pop("prose")
    advance_to_assembly(store, prose=text)
    cfg = store.load_config()
    cfg["word_band_enforce"] = True
    store.save_config(cfg)
    r = submit_output(store, out)
    assert r["verdict"] == "rewrite"
    codes = {v["code"] for v in r["violations"]}
    assert "word_count_low" in codes
    assert not any(w["code"] == "word_count_low" for w in r["warnings"])


def test_pinned_facts_survive_eviction(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    snap = read_json(store.snapshot_path)
    facts = [{"text": f"普通事实{i}", "pin": False} for i in range(1, 13)]
    facts.append({"text": "身世铁律：主角是私生子", "pin": True})
    snap["entities"]["主角"] = {
        "id": "主角",
        "facts": facts,
        "location": "市集",
        "updated_chapter": 1,
    }
    atomic_json(store.snapshot_path, snap)

    # 再入账 3 条新事实：非 pinned 挤掉最早的，pinned 必须还在
    snap2 = apply_event(
        read_json(store.snapshot_path),
        {
            "chapter": 2,
            "state_delta": {
                "facts": [
                    {"who": "主角", "text": "新事实A"},
                    {"who": "主角", "text": "新事实B"},
                    {"who": "主角", "text": "新事实C"},
                ]
            },
        },
    )
    texts = [f["text"] for f in snap2["entities"]["主角"]["facts"]]
    assert "身世铁律：主角是私生子" in texts
    assert "普通事实1" not in texts  # 最早的普通事实被驱逐
    assert len(snap2["entities"]["主角"]["facts"]) <= 12

    # pack 展示里 pinned 优先出现
    atomic_json(store.snapshot_path, snap2)
    pack = assemble_pack(store, 1)
    assert "身世铁律：主角是私生子" in pack["now_card"]["facts"]


def test_plan_extend_appends_chapters(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    before = len(store.load_plan()["chapters"])
    result = store.extend_plan(
        [
            {
                "chapter": 2,
                "location": "账房",
                "present": ["主角"],
                "beats": [
                    {"id": "b1", "required": True, "text": "主角夜里翻账", "must": "翻账"},
                    {"id": "b2", "required": True, "text": "翻账翻出夹页", "must": "翻账"},
                    {"id": "b3", "required": True, "text": "夹页数目对不上", "must": "夹页"},
                    {"id": "b4", "required": True, "text": "掌柜进来打断翻账", "must": "翻账"},
                    {"id": "b5", "required": True, "text": "主角约好明晚再翻账", "must": "翻账"},
                ],
            },
            {
                "chapter": 3,
                "location": "市集",
                "present": ["主角", "掌柜"],
                "beats": [
                    {"id": "b1", "required": True, "text": "凭据找到", "must": "凭据"},
                    {"id": "b2", "required": True, "text": "凭据上的名字被涂了", "must": "凭据"},
                    {"id": "b3", "required": True, "text": "掌柜不认凭据", "must": "凭据"},
                    {"id": "b4", "required": True, "text": "主角抄下凭据存底", "must": "凭据"},
                    {"id": "b5", "required": True, "text": "约明日凭据对质", "must": "凭据"},
                ],
            },
        ]
    )
    assert result["added"] == [2, 3]
    assert result["max_chapter"] == 3
    plan = store.load_plan()
    assert len(plan["chapters"]) == before + 2
    assert [c["chapter"] for c in plan["chapters"]] == sorted(c["chapter"] for c in plan["chapters"])
    # 每章默认 5 场：足数零条数警告；欠数在门口硬拒（见 five_beats 契约测试）
    codes = {w["code"] for w in result["warnings"]}
    assert "beats_count_off_band" not in codes


def test_plan_extend_rejects_non_append_and_empty_beats(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([{"chapter": 1, "beats": [{"id": "b1", "text": "x"}]}])
    assert exc.value.code == "chapter_not_append"
    with pytest.raises(LedgerError) as exc:
        store.extend_plan([{"chapter": 5, "beats": []}])
    assert exc.value.code == "missing_beats"
    # 校验失败不得留下半截写入
    assert max(c["chapter"] for c in store.load_plan()["chapters"]) == 1


def test_plan_extend_cli_reads_chapters_file(tmp_path: Path, capsys):
    """plan extend 只接受 --chapters 文件路径（{chapters:[...]} 或裸列表）。"""
    from novel_ledger_core.control.cli import main

    proj = _make_project(tmp_path)
    chapters_path = tmp_path / "more.json"
    chapters_path.write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "chapter": 2,
                        "location": "账房",
                        "present": ["主角"],
                        "beats": [
                            {"id": "b1", "required": True, "text": "夜里翻账", "must": "翻账"},
                            {"id": "b2", "required": True, "text": "翻账见夹页", "must": "翻账"},
                            {"id": "b3", "required": True, "text": "夹页数目对不上", "must": "夹页"},
                            {"id": "b4", "required": True, "text": "更夫敲梆打断翻账", "must": "翻账"},
                            {"id": "b5", "required": True, "text": "主角决定明夜再翻账", "must": "翻账"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(["plan", "extend", "--project", str(proj), "--chapters", str(chapters_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["added"] == [2]
    assert payload["max_chapter"] == 2
    # CLI 透传；5 场足数零条数警告（欠数只软警告）
    assert payload.get("warnings") == []


def test_plan_extend_five_beats_is_default_four_warned(tmp_path: Path):
    """每章默认 5 场：5 场干净落盘；4 场仍合法但给出软提醒。

    现场依据（生产书 ch9–15）：planner 贴"4–5"下限走，4 场章 draft/polish
    稳定落在 2050–2498 字吃返工；带收成 5–5 后条数警告与字数纪律同向。
    """
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    scenes = [
        {"id": "b1", "required": True, "text": "主角当面拒收改期，掌柜不松手", "must": "拒收"},
        {"id": "b2", "required": True, "text": "凭据还扣着，两人来回争执", "must": "凭据"},
        {"id": "b3", "required": True, "text": "有人催船期，主角走不掉", "must": "船期"},
        {"id": "b4", "required": True, "text": "散场时掌柜丢下一句明天还得来", "must": "明天"},
        {"id": "b5", "required": True, "text": "主角回铺路上盘算下一步找谁作保", "must": "作保"},
    ]
    five = store.extend_plan(
        [{"chapter": 2, "location": "账房", "present": ["主角"], "beats": scenes}]
    )
    assert five["added"] == [2]
    assert five["warnings"] == []
    four = store.extend_plan(
        [{"chapter": 3, "location": "市集", "present": ["主角"], "beats": scenes[:4]}]
    )
    assert four["added"] == [3]
    assert any(item["code"] == "beats_count_off_band" for item in four["warnings"])
    assert max(c["chapter"] for c in store.load_plan()["chapters"]) == 3


def test_plan_extend_rejects_chapters_json_flag(tmp_path: Path, capsys):
    """--chapters-json 已删：再给必须 invalid_args，不能静默当 alternative。"""
    from novel_ledger_core.control.cli import main

    proj = _make_project(tmp_path)
    code = main(["plan", "extend", "--project", str(proj), "--chapters-json", "[]"])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_args"


def test_kb_slice_no_match_is_empty():
    """开篇拍点对不上任何卡时，空切片，不回退灌 power_system / rule 说明书。"""
    cards = [
        {"id": "c-world", "kind": "world", "title": "世界", "body": "世界的描述。"},
        {"id": "c-rule", "kind": "rule", "title": "规则", "body": "越界没有豁免。"},
        {"id": "c-power", "kind": "power_system", "title": "层级", "body": "一阶三层。"},
    ]
    out, _meta = slice_kb(cards, location="无人提及的地点", present=["无名氏"], tags=[], beats=[], cap=2, excerpt_chars=400)
    assert out == []


def test_kb_slice_core_cards_not_auto_included():
    """核心卡不再预留槽：拍点没点名就不进。"""
    cards = [
        {"id": "noise-1", "kind": "world", "title": "无关设定甲", "body": "市集凭据无关的长篇。"},
        {"id": "core-tier", "kind": "power_system", "title": "层级上限", "body": "一阶三层圆满。", "always": True},
        {"id": "core-coin", "kind": "world", "title": "钱币成色", "body": "足色是硬通货。", "priority": "core"},
        {"id": "core-core", "kind": "rule", "title": "总因", "body": "规则跃迁导致天象异变。", "always": True},
    ]
    out, _meta = slice_kb(
        cards,
        location="无人提及的地点",
        present=["无名氏"],
        tags=[],
        beats=[],
        cap=8,
        excerpt_chars=400,
    )
    assert out == []


_OPENING_BEATS = [
    {"id": "b1", "must": "压力", "required": True, "text": "压力先落到身上，来不及解释这个世界"},
    {"id": "b2", "must": "眼前", "required": True, "text": "眼前有人要他当场应对，不能对着空气独白"},
    {"id": "b3", "must": "未完", "required": True, "text": "还有一件未完的事挂着，走不干净"},
]

_WORLD_RULE_CARDS = [
    {
        "id": "总纲--硬-总因",
        "kind": "world",
        "title": "【硬】总因",
        "body": "总因：其本质是规则的指数级跃迁。凡人靠近高阶存在会出异常现象。",
        "tags": ["world", "硬", "总纲"],
        "always": True,
    },
    {
        "id": "总纲--硬-一-宏观异象-天象异变",
        "kind": "world",
        "title": "【硬】一、宏观异象（天象异变）",
        "body": "凡人看去，高阶存在身体边缘泛着诡异的光晕，如同环形光。",
        "tags": ["world", "硬", "总纲"],
    },
    {
        "id": "层级--硬-分级上限表-开篇正常值-数量级",
        "kind": "world",
        "title": "【硬】分级上限表",
        "body": "一阶上限 100–120。三阶 500–600。",
        "tags": ["world", "硬", "层级"],
        "always": True,
    },
    {
        "id": "钱币--硬-品阶与成色-写作尺子-数量级",
        "kind": "world",
        "title": "【硬】品阶与成色",
        "body": "成色分三等。足色是硬通货，兑率约百枚升一级。",
        "tags": ["world", "硬", "钱币"],
        "always": True,
    },
]


def test_kb_slice_opening_beats_skip_world_rule_core():
    """第 1 章这类开篇拍点：不装环形光 / 总因 / 上限表。"""
    out, _meta = slice_kb(
        _WORLD_RULE_CARDS,
        location="边境小城",
        present=["主角", "眼前人"],
        tags=["开场", "边境小城"],
        beats=_OPENING_BEATS,
        extra_text="一名旅人走进边境小城",
        cap=8,
        excerpt_chars=400,
    )
    blob = json.dumps(out, ensure_ascii=False)
    assert "环形光" not in blob
    assert "总因" not in blob
    ids = {c["id"] for c in out}
    assert "总纲--硬-总因" not in ids
    assert "总纲--硬-一-宏观异象-天象异变" not in ids
    assert "层级--硬-分级上限表-开篇正常值-数量级" not in ids


def test_kb_slice_named_term_includes_matching_card():
    """拍点点名术语时，装上对应卡（含 always 核心卡）。"""
    glow, _meta = slice_kb(
        _WORLD_RULE_CARDS,
        location="集市",
        present=["主角"],
        tags=[],
        beats=[{"id": "b1", "must": "环形光", "required": True, "text": "主角看见环形光"}],
        cap=8,
        excerpt_chars=400,
    )
    glow_ids = {c["id"] for c in glow}
    assert "总纲--硬-一-宏观异象-天象异变" in glow_ids
    assert any("环形光" in (c.get("excerpt") or "") for c in glow)

    core, _meta = slice_kb(
        _WORLD_RULE_CARDS,
        location="集市",
        present=["主角"],
        tags=[],
        beats=[{"id": "b1", "must": "总因", "required": True, "text": "有人把总因说破了"}],
        cap=8,
        excerpt_chars=400,
    )
    core_ids = {c["id"] for c in core}
    assert "总纲--硬-总因" in core_ids
    assert any("总因" in (c.get("excerpt") or "") for c in core)

    coin, _meta = slice_kb(
        _WORLD_RULE_CARDS,
        location="集市",
        present=["主角"],
        tags=[],
        beats=[{"id": "b1", "must": "成色", "required": True, "text": "他摸到成色不足的银币"}],
        cap=8,
        excerpt_chars=400,
    )
    coin_ids = {c["id"] for c in coin}
    assert "钱币--硬-品阶与成色-写作尺子-数量级" in coin_ids


def _seed_hooks(store: BookStore) -> None:
    snap = read_json(store.snapshot_path)
    snap["hooks"] = [
        {"id": "h-late", "text": "那块银币不见了", "due": 5, "status": "open", "updated_chapter": 1},
        {"id": "h-soon", "text": "掌柜的承诺", "due": 9, "status": "open", "updated_chapter": 1},
        {"id": "h-far", "text": "远期的伏笔", "due": 40, "status": "open", "updated_chapter": 1},
        {"id": "h-paid", "text": "已应验", "due": 3, "status": "paid", "updated_chapter": 1},
    ]
    atomic_json(store.snapshot_path, snap)


def test_hooks_prioritize_overdue_then_near_due(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    _seed_hooks(store)
    # 第 8 章：h-late 逾期（due 5 < 8），h-soon 临近（due 9），h-far 还远
    out, _omitted = select_hooks(store, chapter=8, cap=5)
    ids = [h["id"] for h in out]
    assert ids[0] == "h-late"
    assert ids[1] == "h-soon"
    assert "h-paid" not in ids
    assert out[0]["due"] == 5


def test_hooks_in_pack_and_delta_merge(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    _seed_hooks(store)
    pack = assemble_pack(store, 1)
    hook_ids = {h["id"] for h in pack["hooks"]}
    assert "h-late" in hook_ids

    # delta 按 id 合并：应验 h-late（置 paid），新埋 h-new
    snap2 = apply_event(
        read_json(store.snapshot_path),
        {
            "chapter": 2,
            "state_delta": {
                "hooks": [
                    {"id": "h-late", "status": "paid"},
                    {"id": "h-new", "text": "新埋的钩子", "due": 20, "status": "open"},
                ]
            },
        },
    )
    hooks = {h["id"]: h for h in snap2["hooks"]}
    assert hooks["h-late"]["status"] == "paid"
    assert hooks["h-new"]["text"] == "新埋的钩子"
    assert len(snap2["hooks"]) == 5


def test_hooks_non_list_rejected(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    out = _short_output(store)
    out["state_delta"]["hooks"] = "not-a-list"
    r = decoupled_submit(store, out)
    assert r["verdict"] == "fix_assembly"
    codes = {v["code"] for v in r["violations"]}
    assert "state_delta_hooks_not_list" in codes


def test_delta_quote_must_be_in_prose(tmp_path: Path):
    """账本增量带 quote 时必须逐字命中正文：幻觉证据只回组装重跑，不丢草稿/终稿。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)

    bad = _short_output(store)
    bad["state_delta"]["facts"] = [
        {"who": "主角", "text": "拒收改期", "quote": "这句话根本不在正文里"}
    ]
    rewritten = decoupled_submit(store, bad)
    assert rewritten["verdict"] == "fix_assembly"
    assert rewritten["phase"] == "await_assembly"
    codes = {v["code"] for v in rewritten["violations"]}
    assert "delta_quote_not_in_prose" in codes
    # 只重组装：草稿与润色终稿必须保留
    assert store.draft_text_path(1).exists()
    assert store.polished_text_path(1).exists()

    good = _short_output(store)
    good["state_delta"]["facts"] = [
        {"who": "主角", "text": "拒收改期", "quote": "主角在市集拒收了改期的凭据"}
    ]
    accepted = decoupled_submit(store, good)
    assert accepted["verdict"] == "accepted"


def test_submit_never_scores_voice(tmp_path: Path):
    """submit 的机检只管结构：不许再有任何文风打分字段或判词混进响应。

    量化那一半删掉之后，文字质感完全交给 ack 子 agent 的人眼判断。响应里若又
    冒出 `voice_score`，说明脚本侧的文风门禁被人悄悄加回来了。
    """
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    # 先走违规路径（故意漏掉 must 词）：打回的响应里同样不许有分数或 voice_* 判词
    bad = _short_output(store)
    bad["prose"] = "什么都没发生。"
    rewritten = decoupled_submit(store, bad)
    assert rewritten["verdict"] == "rewrite"
    assert "voice_score" not in rewritten
    assert not any(str(v.get("code", "")).startswith("voice_") for v in rewritten["violations"])
    # 再走通过路径
    accepted = decoupled_submit(store, _short_output(store))
    assert accepted["verdict"] == "accepted"
    assert "voice_score" not in accepted


def test_stale_pack_reassembles_when_plan_changed(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    old_hash = read_json(store.current_pack_path)["pack_hash"]
    # 装配后改章拍 → 上游指纹失配
    plan = store.load_plan()
    plan["chapters"][0]["beats"] = [{"id": "b9", "required": True, "text": "改拍", "must": "改拍"}]
    store.save_plan(plan)
    r = decoupled_submit(store, _short_output(store))
    assert r["ok"] is True
    assert r["verdict"] == "stale_pack"
    assert r["pack_hash"] != old_hash
    assert store.read_head()["phase"] == "await_draft"
    nxt = chapter_next(store)
    assert nxt["action"] == "draft"  # 重新走整链，不消耗 rewrite 配额


def test_stale_pack_blocks_commit_after_submit(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    r = decoupled_submit(store, _short_output(store))
    assert r["verdict"] == "accepted"
    # submit 通过后、commit 前上游被改 → commit 停线
    plan = store.load_plan()
    plan["chapters"][0]["beats"] = [{"id": "b9", "required": True, "text": "改拍", "must": "改拍"}]
    store.save_plan(plan)
    r2 = chapter_next(store)  # commit 路径
    assert r2["ok"] is True
    assert r2["action"] == "blocked"
    assert store.read_head()["blocked"]["reason"] == "stale_inputs"


def test_relations_merge_and_slice(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    snap = read_json(store.snapshot_path)
    snap["relations"] = [
        {"who": "主角", "target": "掌柜", "kind": "师兄弟", "status": "open", "updated_chapter": 1},
        {"who": "掌柜", "target": "账房掌柜", "kind": "雇佣", "status": "open", "updated_chapter": 1},
        {"who": "主角", "target": "旧敌", "kind": "仇敌", "status": "closed", "updated_chapter": 1},
    ]
    atomic_json(store.snapshot_path, snap)

    out, _omitted = select_relations(store, names=["主角", "掌柜"], cap=6)
    kinds = [(r["who"], r["target"], r["kind"]) for r in out]
    assert ("主角", "掌柜", "师兄弟") in kinds
    assert ("掌柜", "账房掌柜", "雇佣") in kinds  # 双向命中（target 在 names）
    assert not any(k[2] == "仇敌" for k in kinds)  # closed 过滤

    # delta 按 (who,target,kind) 合并 + 关闭
    snap2 = apply_event(
        read_json(store.snapshot_path),
        {
            "chapter": 2,
            "state_delta": {
                "relations": [
                    {"who": "掌柜", "target": "主角", "kind": "师兄弟", "note": "反目"},
                    {"who": "主角", "target": "新盟友", "kind": "结盟", "status": "open"},
                ]
            },
        },
    )
    rels = {(r["who"], r["target"], r["kind"]) for r in snap2["relations"]}
    assert ("掌柜", "主角", "师兄弟") in rels  # 新方向独立记录
    assert ("主角", "新盟友", "结盟") in rels
    assert len(snap2["relations"]) == 5


def test_relations_in_pack(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    snap = read_json(store.snapshot_path)
    snap["relations"] = [{"who": "主角", "target": "掌柜", "kind": "师兄弟", "status": "open"}]
    atomic_json(store.snapshot_path, snap)
    pack = assemble_pack(store, 1)
    assert any(r["kind"] == "师兄弟" for r in pack["relations"])


def test_init_does_not_mutate_default_config(tmp_path: Path):
    from novel_ledger_core.infra.store import DEFAULT_CONFIG

    band_before = dict(DEFAULT_CONFIG["word_band"])
    caps_before = dict(DEFAULT_CONFIG["pack_caps"])
    plan_path = tmp_path / "init-plan.json"
    plan_path.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    proj = tmp_path / "p1"
    proj.mkdir()
    init_project(proj, plan_path=plan_path, protagonist="主角", word_min=20, word_max=9999)
    assert DEFAULT_CONFIG["word_band"] == band_before  # init 参数不得污染全局默认
    assert DEFAULT_CONFIG["pack_caps"] == caps_before
    # 第二次 init 不带 word_min：项目默认仍是 2500 而不是被上一次污染成 20
    proj2 = tmp_path / "p2"
    proj2.mkdir()
    init_project(proj2, plan_path=plan_path, protagonist="主角")
    cfg2 = BookStore(proj2).load_config()
    assert cfg2["word_band"] == {"min": 2500, "max": 8000}
    assert cfg2["word_band_enforce"] is True


def test_load_config_merges_nested_sections(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["pack_caps"] = {"hooks": 3}  # 只写部分字段
    cfg["word_band"] = {"min": 1200}
    store.save_config(cfg)
    merged = store.load_config()
    assert merged["pack_caps"]["hooks"] == 3
    assert merged["pack_caps"]["voice_concepts"] == 24  # 嵌套合并保留默认
    assert merged["word_band"] == {"min": 1200, "max": 8000}


def test_retry_authorize_resets_reopen_quota(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["reopen_limit"] = 1
    store.save_config(cfg)

    def write_and_commit():
        chapter_next(store)
        pack = read_json(store.current_pack_path)
        out = _short_output(store)
        out["pack_hash"] = pack["pack_hash"]
        r = decoupled_submit(store, out)
        assert r["verdict"] == "accepted"
        chapter_next(store)  # commit

    # 第一轮：P0 触发 reopen（配额 1 → reopen_count=1）
    write_and_commit()
    r1 = ack_read(store, quotes=["主角在市集拒收", "市集拒收了改期", "拒收了改期的凭据"], verdict="p0")
    assert r1["verdict"] == "reopen"
    # 第二轮：再 P0 → 配额用尽 → blocked
    write_and_commit()
    r2 = ack_read(store, quotes=["主角在市集拒收", "市集拒收了改期", "拒收了改期的凭据"], verdict="p0")
    assert r2["verdict"] == "blocked"
    # 人授权 → reopen 配额必须重置，第三轮 P0 还能重写一次而不是立即 blocked
    from novel_ledger_core.control.pipeline import retry_authorize

    auth = retry_authorize(store, actor="human", reason="再来一次")
    assert auth["verdict"] == "authorized"
    write_and_commit()
    r3 = ack_read(store, quotes=["主角在市集拒收", "市集拒收了改期", "拒收了改期的凭据"], verdict="p0")
    assert r3["verdict"] == "reopen"
    assert store.read_head()["reopen_count"] == 1


def test_hooks_string_due_passthrough(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    snap = read_json(store.snapshot_path)
    snap["hooks"] = [{"id": "h1", "text": "银币不见了", "due": "第 8 章", "status": "open"}]
    atomic_json(store.snapshot_path, snap)
    out, _omitted = select_hooks(store, chapter=5, cap=5)
    assert out[0]["due"] == "第 8 章"  # 字符串 due 不崩、原样透传


def test_status_reports_plan_remaining(tmp_path: Path):
    """计数与列表是两个字段：能跟 plan_low_water 比大小的只有 `plan_remaining_count`。

    早前只有一个 `plan_remaining`（列表）与 `plan_low_water`（整数）并列在 status 输出里，
    编排者拿两者直接比较——Python 抛 TypeError、JavaScript 静默转 true，无人值守时一直误判水位。
    """
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    from novel_ledger_core.control.pipeline import status

    st = status(store)
    assert st["plan_max_chapter"] == 1
    assert st["plan_remaining_chapters"] == [1]
    assert st["plan_remaining_count"] == 1
    assert "plan_remaining" not in st  # 类型歧义的旧字段名不许回归
    # 与 plan_low_water 同类型，可直接比大小而不抛 TypeError
    assert isinstance(st["plan_remaining_count"], int)
    assert st["plan_remaining_count"] > int(st["plan_low_water"] or 0)


def test_ledger_verify_consistent_and_detects_tamper(tmp_path: Path):
    from novel_ledger_core.ledger.ledger import verify_ledger

    proj = _make_project(tmp_path)
    store = BookStore(proj)
    # 入账一章，账本应一致
    from novel_ledger_core.ledger.ledger import commit_event

    commit_event(
        store,
        1,
        {"moves": [{"who": "主角", "to": "市集"}], "facts": [], "debts": [], "hooks": [],
         "relations": [], "named": ["主角"], "new_names": []},
    )
    # 已入账章必须有正文文件（verify 的交叉校验之一）
    _ch = store.chapter_md_path(1)
    _ch.parent.mkdir(parents=True, exist_ok=True)
    _ch.write_text("主角在市集。\n", encoding="utf-8")
    result = verify_ledger(store)
    assert result["consistent"] is True
    assert result["replayed_chapter"] == 1
    assert result["snapshot_entities"] == 1

    # 篡改 snapshot → 重放对照发现
    snap = read_json(store.snapshot_path)
    snap["entities"]["主角"]["location"] = "被篡改的地点"
    atomic_json(store.snapshot_path, snap)
    tampered = verify_ledger(store)
    assert tampered["consistent"] is False
    assert "entities" in tampered["diffs"]


def test_ack_hint_is_read_only_integrity_check(tmp_path: Path):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    r = decoupled_submit(store, _short_output(store))
    assert r["verdict"] == "accepted"
    ack = chapter_next(store)
    assert ack["action"] == "ack"
    assert ack["quote_requirements"]["minimum"] == 3
    assert "assembly plot self-check plus polish machine/content gates" in ack["hint"]
    assert "pack beats" not in ack["hint"] and "ledger facts" not in ack["hint"]
    assert "voice_skill_manual" not in ack
