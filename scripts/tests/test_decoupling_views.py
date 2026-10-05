"""阶段视图解耦的回归测试。

锁定的不变量：
- 内容写作视图（draft）携带所选文风的 .content.md 手册，没有润色手册、机检或 JSON 契约字段；
- 润色视图（polish）携带所选文风的总手册与内容锚点，键名是 voice_writing_text（与 canonical pack 同名同义）；
- 视图走保序序列化，稳定块（手册/契约）排在文件最前，形成可被 prompt cache 命中的稳定前缀；
- canonical pack 仍是哈希与账本唯一权威，chapter next 同时返回各阶段视图的路径。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.pipeline import chapter_next
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json
from novel_ledger_core.voice.voice_manual import voice_manual_path
from novel_ledger_core.content.views import (
    make_assemble_view,
)

# 组装阶段真正会读的内容字段：
# - 机检：beats、now_card、present_cards、state_near；
# - compact 情节自检：kb_slice、world_spine、character_continuity、glossary。
_GATE_VISIBLE_CONTENT_KEYS = {
    "beats",
    "now_card",
    "present_cards",
    "state_near",
    "kb_slice",
    "character_continuity",
}

# 视图层内容键（views._CONTENT_KEYS 的副本；测试用它做"该有/不该有"的判定）
_ALL_CONTENT_KEYS = {
    "now_card", "beats", "kb_slice", "state_near", "previous_chapter_tail",
    "present_cards", "character_continuity", "debts", "hooks", "relations",
    "near_summaries", "volume_spine", "world_spine", "glossary", "recap",
    "story_focus",
}


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


_VOICE_OR_MECHANIC_KEYS = (
    "instruction",
    "voice_concepts",
    "voice_manual_text",
    "voice_writing_text",
    "voice_writing_manual",
    "voice_checklist",
    "voice_skill_manual",
    "style_formula",
    "pack_hash",
    "inputs_fingerprint",
    "write_contract",
)

_ASSEMBLE_NO_STYLE_KEYS = (
    "instruction",
    "voice_concepts",
    "voice_manual_text",
    "voice_writing_text",
    "voice_writing_manual",
    "voice_checklist",
    "voice_skill_manual",
    "style_formula",
)


def test_draft_view_has_zero_style_and_mechanics(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    resp = chapter_next(store)
    assert resp["action"] == "draft"
    draft = read_json(store.draft_pack_path(1))
    for key in _VOICE_OR_MECHANIC_KEYS:
        assert key not in draft, f"draft view must not carry {key}"
    assert draft["schema"] == "novel-ledger.view.draft.v2"
    assert draft["view"] == "draft"
    assert "writing_brief" in draft
    content_path = voice_manual_path("shijing").with_name("shijing.content.md")
    assert draft["voice_content_text"] == content_path.read_text(encoding="utf-8").strip()
    assert draft["voice_content_manual"] == str(content_path)
    for key in _ALL_CONTENT_KEYS | {"kb_slice_meta", "memory_layers", "items", "conditions"}:
        assert key not in draft, f"draft view must render {key} into writing_brief instead of resending JSON"
    assert "主角拒收改期" in draft["writing_brief"]
    assert "必须自然出现“拒收”" in draft["writing_brief"]
    assert "市集" in draft["writing_brief"]
    assert "draft_contract" in draft
    joined = " ".join(draft["draft_contract"]["gates"])
    assert "文风手册" not in joined and "voice" not in joined


def test_polish_view_uses_the_canonical_voice_manual(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    canon = read_json(store.current_pack_path)
    polish = read_json(store.polish_pack_path(1))
    assert polish["schema"] == "novel-ledger.view.polish.v1"
    assert polish["view"] == "polish"
    assert "pack_hash" not in polish
    assert "write_contract" not in polish
    assert "voice_writing_text" in canon
    assert polish["voice_writing_text"] == canon["voice_writing_text"]
    main_path = voice_manual_path("shijing")
    assert polish["voice_writing_text"] == main_path.read_text(encoding="utf-8")
    assert polish["voice_writing_manual"] == canon["voice_writing_manual"] == str(main_path)
    assert "voice_manual_text" not in canon
    assert "voice_content_text" not in polish
    assert "polish_contract" in polish
    joined = " ".join(polish["polish_contract"]["gates"])
    assert "JSON" not in joined and "pack_hash" not in joined


def test_assemble_view_has_mechanics_but_no_style(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    canon = read_json(store.current_pack_path)
    assemble = read_json(store.assemble_pack_path(1))
    for key in _ASSEMBLE_NO_STYLE_KEYS:
        assert key not in assemble, key
    assert assemble["schema"] == "novel-ledger.view.assemble.v2"
    assert assemble["view"] == "assemble"
    assert assemble["pack_hash"] == canon["pack_hash"]
    assert "write_contract" in assemble
    assert any("pack_hash" in g for g in assemble["write_contract"]["gates"])


def test_next_returns_decoupled_paths_and_hashes(tmp_path: Path):
    proj = _project(tmp_path)
    store = BookStore(proj)
    resp = chapter_next(store)
    canon = read_json(store.current_pack_path)
    assert resp["action"] == "draft"
    assert resp["draft_pack_path"] == str(store.draft_pack_path(1))
    assert resp["draft_output_path"] == str(store.draft_text_path(1))
    assert Path(resp["draft_pack_path"]).exists()
    assert Path(store.polish_pack_path(1)).exists()
    assert Path(store.assemble_pack_path(1)).exists()
    assert "pack_hash" not in resp  # 哈希只在 assemble 视图给
    assemble = read_json(store.assemble_pack_path(1))
    assert assemble["pack_hash"] == canon["pack_hash"]


# --------------------------------------------------------------------------- #
# 视图成本裁剪：每个视图只装该阶段用得上的内容。
# 这三条测试同时是**防回退守卫**——被裁掉的字段都曾经躺在视图里白花上下文钱，
# 而且查看起来"更全"，很容易被后来者当成缺陷补回去。
# --------------------------------------------------------------------------- #


def test_assemble_view_carries_exactly_what_the_gate_can_see(tmp_path: Path):
    """事实编辑视图只装提交机检与 compact 情节自检真正要用的字段。

    compact 档没有独立剧情审校，事实编辑要按 kb_slice / world_spine /
    character_continuity / glossary 做拍点、能力边界、连续性与账目口径核验；
    上章尾、近章摘要不以原字段进入本视图；compact 只收到有界 verification_brief，
    债务/伏笔/关系只收到 ledger_refs 最小投影。

    now_card / present_cards / state_near **必须留着**：它们是
    `pack.allowed_names()` 的全部来源，机检用它校验 `named` / `moves[].who`；
    另给显式 allowed_delta_names，降低 unnamed_in_pack 这一历史头号返工源。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    assemble = read_json(store.assemble_pack_path(1))

    present = _ALL_CONTENT_KEYS & set(assemble)
    assert present == _GATE_VISIBLE_CONTENT_KEYS, (
        f"assemble 视图的内容字段应为 {sorted(_GATE_VISIBLE_CONTENT_KEYS)}，实得 {sorted(present)}"
    )
    assert set(assemble["allowed_delta_names"]) == {"主角", "掌柜"}
    # 明确点名被裁掉的几个（防止"更全一点"式回退）
    for key in ("previous_chapter_tail", "near_summaries", "debts", "hooks", "relations"):
        assert key not in assemble, f"assemble 视图不应直接带原字段 {key}"


def test_allowed_names_covers_ledger_known_characters_not_just_present_ones():
    """`allowed_delta_names` 的判据是"账本已经认识它"，不是"本章刚好在场"。

    现场曾出现：某角色在第 2、6 章都登过场，本章只在
    `state_near.recent` 里出现，`allowed_names()` 只认 now_card + present_cards +
    state_near.pinned，于是 assemble 连报 3 次 `unnamed_in_pack`，worker 只能把她塞进
    `new_names` 才过检——而 `new_names` 的语义是"本章首次登场"，用它兜底会污染首次登场记录。
    """
    from novel_ledger_core.content.pack import allowed_names

    # 基线三条来源照旧
    assert {"主角", "账房", "老药工"} <= allowed_names(
        {
            "now_card": {"name": "主角"},
            "present_cards": [{"name": "账房"}],
            "state_near": {"pinned": [{"id": "老药工"}]},
        }
    )

    # 每个新增来源单独验一遍：混在一个包里会互相掩护（变异注入只砍掉一路时守卫仍绿）。
    assert "商听雪" in allowed_names(
        {"now_card": {"name": "主角"}, "state_near": {"recent": [{"id": "商听雪"}]}}
    )
    assert "商听雪" in allowed_names(
        {
            "now_card": {"name": "主角"},
            "ledger_refs": {
                "relations": [{"who": "主角", "target": "商听雪", "kind": "私诊", "status": "open"}]
            },
        }
    )
    assert "苏青梧" in allowed_names(
        {"now_card": {"name": "主角"}, "character_continuity": [{"name": "苏青梧"}]}
    )


def test_assemble_uses_bounded_ledger_refs_and_compact_verification_brief():
    pack = {
        "chapter": 8,
        "debts": [{"id": "d1", "who": "主角", "text": "归还凭据", "status": "open", "due": 9,
                   "private_note": "不应泄漏"}],
        "hooks": [{"id": "h1", "text": "柜底的信", "status": "open", "due": 10}],
        "relations": [{"who": "主角", "target": "掌柜", "kind": "旧交", "status": "open"}],
        "previous_chapter_tail": "掌柜把凭据压回柜底。",
        "near_summaries": [{"chapter": 7, "l1_summary": "主角没能取回凭据。"}],
        "character_continuity": [{"continuity_directive": "掌柜与主角并非初见。"}],
        "omitted": {"hooks": 2},
    }
    view = make_assemble_view(pack)

    assert view["ledger_refs"]["debts"] == [
        {"id": "d1", "who": "主角", "text": "归还凭据", "status": "open", "due": 9}
    ]
    assert "private_note" not in str(view["ledger_refs"])
    assert "掌柜把凭据压回柜底" in view["verification_brief"]
    assert "主角没能取回凭据" in view["verification_brief"]
    assert "omitted_hint" not in view
    assert "不得浏览整库" in view["omitted_policy"]


def test_assemble_replaces_unsafe_omitted_hint_with_stage_policy():
    pack = {
        "chapter": 1,
        "omitted": {"hooks": 1},
        "omitted_hint": "请用 ledger command 查询全量数据",
    }
    assemble = make_assemble_view(pack)

    assert "omitted_hint" not in assemble
    assert "总编辑" in assemble["omitted_policy"]


def test_polish_view_drops_canon_and_far_context(tmp_path: Path):
    """文风编辑视图不带正典切片与近章摘要，但仍带 must 锚点与字数带。

    该阶段的契约是"内容冻结、只改文风"：正典不是它的判据，把正典递到它面前
    反而是在邀请它去推理剧情（越界）。缺了正典会不会让它"顺手改世界细节"？
    POLISH_GATES 里已用一句显式指令替代了那份隐含数据：
    不得依据一般常识改写世界细节，拿不准一律原样保留。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    polish = read_json(store.polish_pack_path(1))

    for key in (
        "kb_slice",
        "near_summaries",
        "debts",
        "hooks",
        "relations",
        "character_continuity",
        "volume_spine",
        "world_spine",
        "recap",
        "state_near",
    ):
        assert key not in polish, f"polish 视图不该带 {key}（文风重构无引用、纯成本）"
    # 该留的一个都不能少：beats（must锚点）、word_band、voice_writing_text（润色手册）、now_card（人设语气）与 previous_chapter_tail（接榫）
    for key in ("beats", "word_band", "voice_writing_text", "now_card", "previous_chapter_tail"):
        assert key in polish, f"polish 视图必须保留 {key}"
    joined = " ".join(polish["polish_contract"]["gates"])
    assert "不含正典" in joined, "裁掉正典后，必须用显式指令替代那份隐含数据"


def test_views_never_repoint_manual_text_keys(tmp_path: Path):
    """视图不得把 canonical 的"手册内文"键换指另一本文档（M4 事故形态）。

    polish 视图曾把润色手册塞进 `voice_manual_text`，而另一视图里同一个键指向
    总手册。外部文风若另有 writer companion，同名异义仍会误导读者。

    规则：`voice_manual_text`（旧完整手册）/ `voice_writing_text`（润色手册）/ `voice_content_text`
    （内容层）三个键，要么不出现在视图里，要么与 canonical pack 逐字一致——同一个键在不同
    视图里必须指同一份内容；要换层就换键名。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    canon = read_json(store.current_pack_path)

    views = {
        "draft": store.draft_pack_path(1),
        "polish": store.polish_pack_path(1),
        "assemble": store.assemble_pack_path(1),
    }
    for name, path in views.items():
        view = read_json(path)
        for key in ("voice_manual_text", "voice_writing_text", "voice_content_text"):
            if key in view:
                assert view[key] == canon.get(key), (
                    f"{name} 视图把 {key} 换指了另一本文档：同一个键在不同视图里必须是同一份内容"
                )


def test_polish_view_keeps_legacy_handwritten_pack_fallback():
    """手写旧 pack 只有 voice_manual_text 时，润色视图仍能读到手册内文。"""
    from novel_ledger_core.content.views import make_polish_view

    view = make_polish_view({"chapter": 1, "voice_manual_text": "旧版完整手册"})
    assert view["voice_writing_text"] == "旧版完整手册"
    assert "voice_manual_text" not in view
    assert "voice_writing_manual" not in view


def test_polish_view_keeps_glossary_when_present():
    """当项目配置了 glossary 时，polish 视图必须保留它，以便文风编辑规避或纠正违禁术语。"""
    from novel_ledger_core.content.views import make_polish_view

    pack = {
        "schema": "novel-ledger.pack.v1",
        "chapter": 1,
        "beats": [{"id": "b1", "text": "戏"}],
        "voice_writing_text": "文风手册",
        "word_band": {"min": 800, "max": 6000},
        "now_card": {"name": "主角"},
        "previous_chapter_tail": "上章末尾",
        "glossary": {"旧术语": "新规范术语"},
    }
    view = make_polish_view(pack)
    assert "glossary" in view
    assert view["glossary"] == {"旧术语": "新规范术语"}
    assert "now_card" in view
    assert "previous_chapter_tail" in view


def test_canon_dependent_stages_receive_kb_without_duplicate_draft_json(tmp_path: Path):
    """执笔通过完整句式接收正典，事实编辑继续保留结构化正典切片。

    canonical KB 仍是唯一真源；draft 的 `writing_brief` 是脚本单向渲染结果，不能再把
    同一批卡片以 `kb_slice` JSON 重发。assemble 的情节自检要逐卡核验，保留结构化字段。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    draft = read_json(store.draft_pack_path(1))
    assemble = read_json(store.assemble_pack_path(1))
    assert "kb_slice" not in draft
    assert "## 正典规则" in draft["writing_brief"]
    assert "kb_slice" in assemble


def test_draft_brief_renders_structures_as_complete_sentences_without_losing_constraints():
    from novel_ledger_core.content.views import make_draft_view

    pack = {
        "chapter": 9,
        "word_band": {"min": 2200, "max": 2800},
        "now_card": {
            "name": "沈砚",
            "goal": "查清失踪账册",
            "location": "南仓",
            "wound": "左臂未愈",
            "facts": ["已经见过掌柜"],
        },
        "beats": [
            {"required": True, "text": "沈砚拒绝交出钥匙", "must": "旧账"},
            {"required": True, "text": "巡夜人封锁南仓", "must": "明日"},
        ],
        "previous_chapter_tail": "仓门在他身后合上。",
        "kb_slice": [
            {"id": "warehouse", "title": "南仓门禁", "excerpt": "日落后只有铜牌能开仓门。"}
        ],
        "kb_slice_meta": {
            "matched_cards": 3,
            "selected_cards": 1,
            "omitted_cards": 2,
            "explicit_refs": ["warehouse"],
        },
        "memory_layers": {
            "phase_summary": {"summary": "沈砚正在追查被篡改的仓储账目。"},
            "volume_summary": {"summary": "粮案逐渐牵出城中官商勾连。"},
            "book_spine": {"summary": "主角以账目证据撬动旧秩序。"},
        },
        "story_focus": {
            "stage": "查账幕",
            "threads": [{
                "id": "thread-ledger", "kind": "main", "name": "失踪账册",
                "purpose": "追出账册去向", "mainline_link": "账册是撬动旧秩序的证据",
                "stage_touchpoints": [{"event": "找到改账痕迹", "change": "调查转向城中官商"}],
            }],
            "clocks": [{
                "id": "clock-world", "kind": "world", "name": "封账倒计时", "start_state": "三日后封账",
                "stage_events": [{"event": "仓储账册封存", "deadline": "三日后", "consequence": "证据灭失"}],
            }],
            "tension": {
                "level": 4, "mode": "reversal", "pressure": "巡夜人封仓", "turn": "找到改账痕迹",
                "payoff": "确认内鬼存在", "next_imbalance": "内鬼开始反查",
            },
        },
        "items": [{"id": "key", "name": "铜钥匙", "holder": "沈砚", "quantity": 1, "status": "held"}],
        "state_near": {"pinned": [{"id": "沈砚", "location": "南仓", "dead": False}]},
        "glossary": {"库房令牌": "铜牌"},
    }

    view = make_draft_view(pack)
    brief = view["writing_brief"]
    # 字数纪律必须是"安全目标 + 场次尺度 + 越线后果"，只给区间会被写成下限：
    # 现场实测第 5/6/8 章首轮草稿 1789–2309 汉字，全被 precheck 打回再补整场戏。
    # aim=min+700 且不超上限：2200+700=2900 被 max=2800 截住，安全目标=2800。
    assert "本章正文目标长度为2800个中文字（硬闸2200至2800，只数汉字）" in brief
    assert "共 4 场戏，每场约 700 汉字" in brief
    assert "写不足2200会被 `chapter draft-submit` 当场拒收" in brief
    assert "第1场必须完整演出：沈砚拒绝交出钥匙" in brief
    assert "必须自然出现“旧账”" in brief
    assert "上一章结尾原文如下" in brief
    assert "正典卡“南仓门禁”（卡片 id 为 warehouse）" in brief
    assert "另有2张相关候选因工作包上限未注入" in brief
    assert "当前阶段摘要是：沈砚正在追查被篡改的仓储账目。" in brief
    assert "## 本章故事线路与时钟" in brief
    assert "故事线“失踪账册”" in brief
    assert "时间线“封账倒计时”" in brief
    assert "id=thread-ledger" not in brief
    assert "id=clock-world" not in brief
    assert "名称为铜钥匙，持有人为沈砚，数量为1，状态为持有中" in brief
    assert "死亡标记为否" in brief
    assert "False" not in brief and "held" not in brief
    assert "正文不得使用“库房令牌”，必须统一写作“铜牌”" in brief
    for raw_key in ("beats", "now_card", "kb_slice", "kb_slice_meta", "memory_layers", "glossary", "word_band"):
        assert raw_key not in view


def test_views_lead_with_stable_prefix_for_prompt_cache(tmp_path: Path):
    """视图必须把每章一字不差的稳定块（手册/契约）排到最前，供 prompt cache 命中。

    旧行为走 sort_keys=True，字母序把逐章的 `beats` 排到 `voice_*` 之前，
    稳定前缀被逐章变化的内容打断到几乎为零。这里钉死：draft/polish 视图文件里，
    手册字段必须出现在首个逐章变化键（chapter）之前，且稳定前缀占文件相当比例。
    """
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    for path, manual_key in (
        (store.draft_pack_path(1), "voice_content_text"),
        (store.polish_pack_path(1), "voice_writing_text"),
    ):
        raw = path.read_text(encoding="utf-8")
        # 手册必须排在 chapter 之前——否则稳定前缀会被逐章字段截断
        assert raw.index(f'"{manual_key}"') < raw.index('"chapter"'), raw[:120]
        prefix = raw.index('"chapter"')
        assert prefix > len(raw) * 0.5, f"稳定前缀仅占 {prefix}/{len(raw)}，缓存收益不达标"


def test_view_serialization_is_order_preserving_but_pack_hash_is_canonical(tmp_path: Path):
    """保序序列化只作用于视图；canonical pack 仍走排序键，pack_hash 不因视图顺序改变。"""
    proj = _project(tmp_path)
    store = BookStore(proj)
    chapter_next(store)
    canon = read_json(store.current_pack_path)
    # canonical pack 文件本身是排序键（首键应是字母序最小的），视图不是。
    raw_canon = store.current_pack_path.read_text(encoding="utf-8")
    assert raw_canon.index('"beats"') < raw_canon.index('"pack_hash"')  # 排序键特征
    assemble = read_json(store.assemble_pack_path(1))
    assert assemble["pack_hash"] == canon["pack_hash"]


def test_assemble_view_renders_continuity_directive_only_once():
    """连续性指令句只在 verification_brief 渲染一次，原始记录不再逐字二发。

    原始 character_continuity 记录与 verification_brief 曾各自带一份同样的
    continuity_directive 原文——同一句防失忆警告每章双份注入组装视图。
    """
    pack = {
        "chapter": 3,
        "character_continuity": [
            {
                "name": "掌柜",
                "first_seen_chapter": 2,
                "continuity_directive": "【连续性警告】：角色【掌柜】在第 2 章已登场，严禁写成初见。",
            }
        ],
    }
    view = make_assemble_view(pack)

    # 指令句经 verification_brief 渲染成完整句子（情节自检的判据形态）
    assert "严禁写成初见" in view["verification_brief"]
    # 原始记录保留结构字段、剥离指令句；全文只出现一次
    records = view["character_continuity"]
    assert records and records[0]["name"] == "掌柜"
    assert "continuity_directive" not in records[0]
    assert str(view).count("严禁写成初见") == 1
