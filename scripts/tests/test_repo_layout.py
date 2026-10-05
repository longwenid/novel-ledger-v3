"""仓库结构不变量（v3：维护工程类断言——变异/压力/插件钩子——留在 v1 仓库 .dev/，此处只保留 v3 自身结构的断言）。

这些断言不测行为，测的是**目录与文件命名架构**：根目录只留一个入口文档、死模板不复活、
按需文档不脱离入口。它们比注释可靠——注释会过期，测试会红。改结构前先读 `.dev/README.md`
的「结构准则」。
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from tests.conftest import requires_bash

REPO = Path(__file__).resolve().parents[2]
SKILL_MD = REPO / "SKILL.md"
REFERENCES = REPO / "references"
VOICES = REFERENCES / "voices"

# SKILL.md 里指向 references 的相对链接，允许带 #锚点
_REF_LINK_RE = re.compile(r"\]\((references/[^)#\s]+\.md)(?:#[^)]*)?\)")


def test_repo_root_has_only_skill_md():
    """根目录只放 SKILL.md（skill 加载约定）。维护者文档进 .dev/，按需文档进 references/。"""
    found = sorted(p.name for p in REPO.glob("*.md"))
    assert found == ["SKILL.md"], f"根目录多出 .md：{found}"


def test_dead_config_template_stays_deleted():
    """templates/config.json 是死文件：store._load_template_json 从不读它，
    配置真源是 store.DEFAULT_CONFIG。放回来只会长期漂移成一份错的文档。"""
    assert not (REPO / "templates" / "config.json").exists()


def test_default_pipeline_lane_is_compact():
    """单一 compact 泳线：config 默认没有 pipeline_lane 键。"""
    from novel_ledger_core.infra.store import DEFAULT_CONFIG

    assert "pipeline_lane" not in DEFAULT_CONFIG


def test_default_execution_contract_cannot_regress_to_worker_fanout():
    """Default isolation requires fresh serial stage jobs; legacy modes remain explicit."""
    skill = SKILL_MD.read_text(encoding="utf-8")
    # The global compatibility fixture changes the live default; check the
    # deployment source here, and validate actual envelopes in the stage suite.
    source = (REPO / "scripts/novel_ledger_core/infra/store.py").read_text(encoding="utf-8")
    assert '"execution_mode": "stage-agent"' in source
    assert "默认 `stage-agent`" in skill
    assert "禁止继承聊天历史" in skill
    assert "只有职责隔离" in skill
    for rel in ("agents/roles/managing-editor.md", "references/dispatch.md", "references/roles.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "信封" in text


def test_runtime_package_excludes_maintainer_surface():
    """运行时发布包只带必要资产；测试、报告和维护脚本不应上传到用户环境。"""
    ignore = (REPO / ".ferryignore").read_text(encoding="utf-8").splitlines()
    rules = {line.strip() for line in ignore if line.strip() and not line.lstrip().startswith("#")}
    required = {".dev/", "scripts/tests/", "projects/", "Makefile", "pytest.ini"}
    assert required <= rules, f".ferryignore 缺运行时排除项：{sorted(required - rules)}"


def test_execution_architecture_states_host_runner_acceptance_boundary():
    """Skill 指令不能冒充 runner；发布文档必须保留跨会话接线与人工降级路径。"""
    text = (REPO / "references" / "execution-architecture.md").read_text(encoding="utf-8")
    required = (
        "Skill 指令本身不是运行器",
        "spawn_allowed=false",
        "chapter usage-record",
        "ack-read",
        "control_fingerprint",
        "逐请求回报四分量 telemetry",
        "人工按章创建新任务",
        "run start",
    )
    missing = [item for item in required if item not in text]
    assert not missing, f"宿主上线边界文档缺项：{missing}"


def test_quantitative_voice_stays_deleted():
    """文风只剩「手册 → pack」一条线，量化那一半的所有落点都必须保持删除。

    删的理由不是"代码不好"，是判据不硬：九项指标对手册里的视角/幽默/结构/价值观底色
    完全盲视（包括本仓库唯一的自有增量「禁不是…是…对举句」），却能凭 z 值把定性全过的
    章节打回重写。出厂基线本身还是按手册统计表手工构造的二手数据。
    这些文件一旦回来，就说明那条线又被接上了——文字质感现在只由 ack 子 agent 判。
    """
    assert not (REPO / "scripts" / "wenfeng").exists()
    assert not (REPO / "scripts" / "novel_ledger_core" / "wenfeng_clean.py").exists()
    assert not (REPO / "scripts" / "novel_ledger_core" / "voice_wenfeng.py").exists()
    assert not (REPO / "templates" / "wenfeng").exists()
    core = (REPO / "scripts" / "novel_ledger_core" / "voice" / "voice_manual.py").read_text(encoding="utf-8")
    for dead in ("METRICS", "score_prose", "compute_baseline", "fingerprint", "load_baseline"):
        assert dead not in core, dead
    pkg = REPO / "scripts" / "novel_ledger_core" / "control" / "pipeline"
    for mod in sorted(pkg.glob("*.py")):
        text = mod.read_text(encoding="utf-8")
        assert "voice_gate" not in text, mod.name
        assert "voice_score" not in text, mod.name


def test_skill_md_reference_links_resolve():
    """SKILL.md 里每个 references/*.md 链接都要指向存在的文件。删文档先改链接。"""
    links = set(_REF_LINK_RE.findall(SKILL_MD.read_text(encoding="utf-8")))
    assert links, "SKILL.md 里一个 references 链接都没有，渐进式披露断了"
    missing = sorted(rel for rel in links if not (REPO / rel).is_file())
    assert not missing, f"SKILL.md 链接指向不存在的文件：{missing}"


def test_every_reference_doc_is_reachable_from_skill_md():
    """references/ 下每份 .md 都必须能从 SKILL.md 直接链到。

    渐进式披露的方向是「入口 → 按需细节」。出现一份没人链接的 references 文档，等于把内容
    藏起来还要付维护成本，而且它会悄悄和 SKILL.md 漂移成两套说法。
    """
    linked = {rel.split("/", 1)[1] for rel in _REF_LINK_RE.findall(SKILL_MD.read_text(encoding="utf-8"))}
    # 链接里永远是 POSIX 正斜杠；Windows 上 rglob 相对路径是反斜杠，必须先归一化再比，
    # 否则连已链接的文档都会被误判成孤儿。
    on_disk = {str(p.relative_to(REFERENCES)).replace("\\", "/") for p in REFERENCES.rglob("*.md")}
    orphans = sorted(on_disk - linked)
    assert not orphans, f"references 下有 SKILL.md 链不到的文档：{orphans}"


def test_institutional_docs_stay_fragmented():
    """碎片化纪律：每份 md 都有行数上限，超限就按用例拆分。

    - SKILL.md ≤300 行（任务按需路由见 test_policy_conformance）；
    - references（除 voices）≤300 行——制度层按用例碎片化的硬要求；
    - agents/roles 角色卡 ≤250 行（worker 整读单元，不跨文件拆卡）；
    - references/voices 声线手册 ≤400 行、templates ≤200 行——资产层可携带
      内容，限额只防失控膨胀，不逼内容删减。
    contracts.md 曾膨胀到 654 行后被拆成契约族，这条守卫防它再长回来。
    """
    limits = {
        "SKILL.md": 300,
        "references": 300,
        "voices": 400,
        "agents/roles": 250,
        "templates": 200,
    }
    offenders = []

    def _check(path: Path, cap: int) -> None:
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > cap:
            offenders.append(f"{path.relative_to(REPO).as_posix()} 有 {count} 行（上限 {cap}）")

    _check(REPO / "SKILL.md", limits["SKILL.md"])
    for path in sorted(REFERENCES.rglob("*.md")):
        rel = path.relative_to(REFERENCES).as_posix()
        cap = limits["voices"] if rel.startswith("voices/") else limits["references"]
        _check(path, cap)
    for path in sorted((REPO / "agents" / "roles").glob("*.md")):
        _check(path, limits["agents/roles"])
    for path in sorted((REPO / "templates").rglob("*.md")):
        _check(path, limits["templates"])
    assert not offenders, "文档超限——按用例拆分，不要长成第二份总手册：" + "；".join(offenders)


# 项目信息泄漏的「形态指纹」：优化 skill 多发生在跑书会话里，书的上下文会顺着
# 注释与示例溜进发行面。token 黑名单只能拦已知的词；这三类**形态**在任何合法的
# skill 内容里都不该出现，可以无条件拦（合法豁免：scripts/tests 的 ts 夹具用 ISO
# 日期当样本数据，不在扫描面；文件名形态 ch-0001 带连字符且四位，不匹配）。
_PROJECT_FINGERPRINTS = (
    (re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"), "裸日期（事故/出处时间戳）"),
    (re.compile(r"\bsess_[0-9a-f]{8}\b"), "会话 id（审计出处）"),
    (re.compile(r"\bch ?\d{1,3}\b"), "chN 章号叙事（单章事故证据）"),
)


def test_project_fingerprints_stay_out_of_skill():
    """跑书会话里改 skill 时，项目上下文（日期/会话号/章号事故）不得写进发行面。

    人名/书名/题材词没有可靠形态，仍靠 token 黑名单与分层规则兜底：制度层零作品
    内容，项目差异一律走 config/正典注入（glossary、quant_keys、conditions.kind
    是范本）或资产层（templates/backgrounds）。往 skill 落改动时的改写纪律见
    .dev/README.md「改 skill 时的防泄漏纪律」。
    """
    targets = [REPO / "SKILL.md"]
    targets += sorted(REFERENCES.rglob("*.md"))
    targets += sorted((REPO / "agents").rglob("*.md"))
    targets += sorted((REPO / "templates").rglob("*.md"))
    targets += sorted((REPO / "policies").glob("*.json"))
    targets += sorted((REPO / "scripts" / "novel_ledger_core").rglob("*.py"))
    targets.append(REPO / "scripts" / "novel_ledger.py")
    offenders = []
    for path in targets:
        rel = path.relative_to(REPO).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, label in _PROJECT_FINGERPRINTS:
                if pattern.search(line):
                    offenders.append(f"{rel}:{line_no} {label}: {line.strip()[:60]}")
    assert not offenders, "项目指纹混进发行面：" + "；".join(offenders)


# 信封/包会亮出的「需要执行侧裁决」的信号，必须在看到它的那个阶段的卡片上有处置口径。
# 缺口径的现场形态：worker 去读 skill 源码与测试逆向工程语义，烧掉大量推理轮次后章节停摆。
# 新增信封/视图信号时，同步登记进这张注册表（信号 → 必须覆盖的卡片）。
_SIGNAL_DISPOSITIONS = (
    ("hooks_due_unplanned", ("agents/roles/worker-protocol.md", "agents/roles/drafting-editor.md")),
    ("overdue_hooks", ("agents/roles/worker-protocol.md",)),
    ("expected_delta", ("agents/roles/ledger-editor.md",)),
    ("relation_conflicts", ("agents/roles/ledger-editor.md",)),
    ("voice_anchor_text", ("agents/roles/voice-editor.md",)),
)


def test_envelope_signals_have_stage_dispositions():
    """信封/包信号必须有阶段处置口径；worker 拿不准语义停步上报，不读源码反推。

    处置口径 = 「这个信号来了我怎么处置」的操作句（在哪一步、动什么、不动什么），
    写在看到该信号的角色卡上。协议卡另有一条铁律兜底：语义不明时停步上报，
    禁止读 skill 的 python 源码/测试逆向工程——那是轮次预算换猜测。
    """
    for signal, cards in _SIGNAL_DISPOSITIONS:
        for rel in cards:
            text = (REPO / rel).read_text(encoding="utf-8")
            assert signal in text, f"{rel} 缺 `{signal}` 的处置口径"
    proto = (REPO / "agents" / "roles" / "worker-protocol.md").read_text(encoding="utf-8")
    assert "reverse-engineer" in proto, "worker 协议卡缺「禁止读源码反推语义」铁律"


def test_old_single_wenfeng_manual_stays_deleted():
    """单一绑定手册已迁到 references/voices/ 多套抽象手册，旧路径不得复活成第二份真源。"""
    assert not (REFERENCES / "wenfeng.md").exists()
    assert not (REFERENCES / "voices" / "wulong.md").exists()


def test_product_docs_do_not_point_at_deleted_voice_paths():
    """SKILL.md / 契约族文档是 agent 入口，不得再指向已删的单一手册、打分线或旧 flag。"""
    dead = (
        "references/wenfeng.md",
        "voice_wenfeng",
        "voice distill",
        "voice score",
        "voice_gate",
        "templates/wenfeng",
        "人味",
        "--chapters-json",
        "voice_engine",
        "wulong",
        "乌龙山",
        "林河",
        "爱因斯坦环",
        "质能密度",
        "系统架构师",
        # 具体书的场所名（量化口径示例里曾出现，中性化后不得回流）
        "望春楼",
        "聚宝坊",
    )
    doc_targets = ["SKILL.md"] + [
        f"references/{path.relative_to(REFERENCES).as_posix()}"
        for path in sorted(REFERENCES.rglob("*.md"))
        if not path.relative_to(REFERENCES).as_posix().startswith("voices/")
    ]
    for rel in doc_targets:
        text = (REPO / rel).read_text(encoding="utf-8")
        for token in dead:
            assert token not in text, f"{rel} still mentions {token}"


def test_bundled_voices_parse_under_shared_contract():
    """六套随包文风都能由统一解析契约抽出非空公式、概念和自检。"""
    from novel_ledger_core.voice.voice_manual import iter_voice_manuals, parse_skill_md

    manuals = iter_voice_manuals()
    # iter_voice_manuals 返回 list[(voice_id, path)]
    ids = {voice_id for voice_id, path in manuals}
    assert {"shijing", "cinematic", "baimiao", "mystery", "heroic", "lyrical"} <= ids
    assert len(manuals) >= 6
    for voice_id, path in manuals:
        text = path.read_text(encoding="utf-8")
        assert "## 自检" in text, path.name
        assert "### 硬禁" in text, path.name
        meta = parse_skill_md(path)
        assert meta["style_formula"], path.name
        assert meta["concepts"], path.name
        assert meta["checklist"], path.name


def test_stage_gates_stay_voice_and_genre_neutral():
    """阶段契约（draft/polish gates）不得写死某一套文风或题材。

    文风与内容判据必须来自所选手册/项目正典，而不是 views.py 里的一条固定文案；
    否则换 cinematic 或非仙侠项目时会拿市井烟火/修真设定去卡正文。
    """
    views = (REPO / "scripts" / "novel_ledger_core" / "content" / "views.py").read_text(encoding="utf-8")
    leaked = (
        "市井烟火",
        "冷面荒诞",
        "柴米",
        "修炼破境",
        "凡人绝无内视神识",
        "湿木炼丹",
        "修真货币",
        "杂役待遇",
        "活计单章",
        "谈判拆账",
    )
    for token in leaked:
        assert token not in views, f"stage gate must not hard-code voice/genre content: {token}"


def test_shijing_manual_starts_with_rules():
    """手册给写者/ack 看，开头必须是文风标题，解析契约在 .dev/architecture.md。"""
    text = (VOICES / "shijing.md").read_text(encoding="utf-8")
    assert text.lstrip().startswith("# 市井烟火文风")  # 允许标题有后缀（生产手册、手册等）
    assert "改本文件前必读" not in text
    assert "## 自检" in text
    assert "### 硬禁" in text


def test_intent_template_and_creative_card_stay_content_neutral():
    """意图书模板与创意编辑卡不得回灌具体作品的世界观/人物/数值。

    曾内嵌一部具体书的完整提案（书名、主角名、宗族、境界道具等）后被移除；
    这些 token 一旦复活，等于制度层又背上一本书的内容。
    """
    dead = (
        "九品斩妖吏",
        "陈平安",
        "百两血砂",
        "清河镇",
        "赵氏宗族",
        "筑基残片",
        "Blocked 解锁",
    )
    template = REPO / "templates" / "intent.example.md"
    card = REPO / "agents" / "roles" / "creative-editor.md"
    for path in (template, card):
        text = path.read_text(encoding="utf-8")
        for token in dead:
            assert token not in text, f"{path.name} still mentions {token}"
    assert "模板与填写口径" in template.read_text(encoding="utf-8")


def test_hatch_guidance_stays_genre_neutral():
    """开书制度与空白模板不得把某一题材的词汇当成所有作品的默认必选项。"""
    targets = (
        REPO / "agents" / "roles" / "creative-editor.md",
        REFERENCES / "hatch-wizard.md",
        REFERENCES / "dispatch.md",
        REPO / "templates" / "intent.example.md",
        REPO / "templates" / "outline.example.md",
    )
    genre_defaults = (
        "仙侠",
        "飞升",
        "天道",
        "修炼",
        "神识",
        "阵法",
        "宗门",
        "灵药",
        "金手指",
        "后宫",
        "女主",
        # 中性化补漏：示例枚举与措辞里曾漏进来的修仙系词
        "修真",
        "修为",
        "修士",
        "散修",
        "门派",
        "灵石",
        "道友",
        "境界",
    )
    # 注：「修仙」不在表内——hatch-wizard 的 `background: xianxia-mortal` 预置背景
    # 描述（选装模板本身）合法保留；被禁的是把修仙词汇当成所有作品的默认。
    for path in targets:
        body = path.read_text(encoding="utf-8")
        for token in genre_defaults:
            assert token not in body, f"{path.relative_to(REPO)} hard-codes genre default: {token}"


def test_hatch_guidance_is_low_input_v3_without_legacy_escape_hatch():
    wizard = (REFERENCES / "hatch-wizard.md").read_text(encoding="utf-8")
    skill = (REPO / "SKILL.md").read_text(encoding="utf-8")
    cli = (REPO / "scripts" / "novel_ledger_core" / "control" / "cli.py").read_text(encoding="utf-8")
    for marker in ("extra_<slug>", "A/B/C", "换一组", "--check-only", "novel-ledger.hatch.v3"):
        assert marker in wizard
    # 决策门形态锚：低输入向导必须保持「九个基础门 + 按需补充」的短选择链形态，
    # 不得退回逐要素自由问卷（变异 M88 的守卫位）。
    assert "## 二、九个基础决策门与按需补充" in wizard
    assert "worker 不派子 agent" in skill
    assert "--legacy-import" not in cli
    assert "novel-ledger.hatch.v2" not in wizard


def test_tension_craft_manual_and_planning_card_stay_content_neutral():
    """张力与兑现手册、章拍样例与策划编辑卡也不得回灌具体作品内容。

    这三份是制度层内容中性文档：手册只讲方法，样例只演示编排。任何一本书的
    书名/人物/专有数值一旦复活，等于把某本书的内容又固化进 skill。
    """
    dead = (
        "九品斩妖吏",
        "陈平安",
        "百两血砂",
        "清河镇",
        "赵氏宗族",
        "筑基残片",
        "李明",
        "Bug修复系统",
        "赛博职场",
        "Blocked 解锁",
    )
    targets = (
        REFERENCES / "craft" / "tension-payoff.md",
        REPO / "agents" / "roles" / "planning-editor.md",
        REPO / "templates" / "plan.chapters.example.json",
        REPO / "templates" / "outline.example.md",
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for token in dead:
            assert token not in text, f"{path.name} still mentions {token}"

# 制度层（SKILL.md / references / agents / hooks 文档）必须零作品内容。
# 资产层（references/voices/** 手册、templates/** 种子）按定义可以带内容，不在扫描范围。
_INSTITUTIONAL_DOCS = ("SKILL.md",)
_CONTENT_NEUTRAL_SKIP = ("references/voices/", "templates/")


def _institutional_doc_paths():
    """枚举制度层里由 agent 阅读的 Markdown；资产层与维护者目录不参与。"""
    found = [REPO / name for name in _INSTITUTIONAL_DOCS]
    for base in ("references", "agents"):
        for path in sorted((REPO / base).rglob("*.md")):
            rel = str(path.relative_to(REPO))
            if any(rel.startswith(prefix) for prefix in _CONTENT_NEUTRAL_SKIP):
                continue
            found.append(path)
    return found


def test_institutional_layer_holds_no_work_content():
    """制度层不得携带任何一部作品的世界观、人物、专名或外部作品名。

    第三轮清理：创意编辑卡曾内嵌三个「高级推演模式」的完整作品样例
    （一个修仙×法庭的融合设定、一个职场金手指的十章节拍表，外加若干现实作品名），
    占卡长 280+ 行。删的理由有两条，缺一不可：
    1. roles.md §0 规定制度层零作品内容——那些样例本身就是一部书的设定；
    2. 每次派发创意编辑都要读这 280 行，而正常推演根本用不到（已移进
       creative-editor.advanced.md，只在点名授权时加载）。

    新增角色卡时要问一句：这段文字是**方法**还是**某一部书的内容**？
    方法留下，内容进项目层或资产层。
    """
    leaked = (
        # 修仙×法庭融合样例
        "因果律法庭",
        "因果镜",
        "业力律师",
        "因果回溯",
        "夺舍冤案",
        # 职场金手指样例
        "打工人",
        "Bug修复系统",
        "赛博职场",
        "技术宅",
        "李明",
        # 现实作品名（举例即携带外部内容，方法论不依赖它们）
        "死侍",
        "苏菲的世界",
        "楚门的世界",
        "银河系搭车客指南",
        "如果在冬夜，一个旅人",
        # 声线手册定位里曾出现的标杆书名（中性化后不得回流）
        "凡人修仙传",
    )
    offenders = []
    for path in _institutional_doc_paths():
        text = path.read_text(encoding="utf-8")
        for token in leaked:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert not offenders, "制度层混入作品内容：" + "；".join(offenders)


def test_dispatch_docs_do_not_restate_style_rules():
    """派发侧文档（角色卡 + dispatch.md）不得复述任何具体文风条目。

    SKILL.md / 契约族文档 **可以**记录机检闸门的实现（它们描述脚本行为）；
    但派发侧文档一旦写出禁词、句长、标点、配额这些**条目实例**，就会在手册更新后
    漂移成第二真源——这正是历史事故的成因（prompt 里抄了硬禁清单，手册改了两边对不上）。

    注意：写「手感、禁词、句长、标点、收尾」这类**类别名**是允许的（那是在讲纪律），
    这里拦的是**条目实例**。
    """
    restated = (
        "分号",
        "AI死词",
        "现代IT",
        "IT词",
        "四字定格",
        "四字神话",
        "空灵收尾",
        "对话占比",
        "对仗腔",
        "生理苦难",
    )
    paths = sorted((REPO / "agents").rglob("*.md")) + [REPO / "references" / "dispatch.md"]
    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in restated:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert not offenders, "派发侧文档复述了具体文风条目：" + "；".join(offenders)


def test_advanced_role_docs_are_reachable_from_their_base_card():
    """角色卡的补充文档必须从主卡链到。

    渐进式披露的方向是「主卡 → 按需细则」。creative-editor.advanced.md 的加载方式是
    「总编辑点名授权时才把路径写进 prompt」，所以主卡里必须有那条索引，
    否则这份细则会变成一份谁也想不起来读的死文档，而主卡上的能力说明会慢慢和它漂移。

    判定必须是**可点击的链接**，不能只是正文里提到文件名——提到名字但没链接，
    agent 读主卡时仍然过不去（本测试的变异验证里，只提名字是拦不住的）。
    """
    for base in sorted((REPO / "agents").rglob("*.md")):
        stem = base.name[: -len(".md")]
        if "." not in stem:
            continue  # 主卡（如 creative-editor.md）无需被链
        # 补充文档命名形如 <base>.<extra>.md
        main_name = stem.split(".", 1)[0] + ".md"
        main = base.with_name(main_name)
        assert main.is_file(), f"补充文档 {base.name} 没有对应的主卡 {main_name}"
        linked = {
            (main.parent / rel).resolve()
            for rel in _MD_LINK_RE.findall(main.read_text(encoding="utf-8"))
            if not rel.startswith(("http://", "https://", "/"))
        }
        assert base.resolve() in linked, (
            f"{main_name} 里没有可点击的链接指向 {base.name}（只提文件名不算），"
            "按需细则会变成死文档"
        )


def test_role_cards_are_indexed_and_exist():
    """每张角色操作卡都必须由 `roles.md` 的 token→文件名索引可达，且索引不指向空。

    历史缺陷：`roles.md` 与 `SKILL.md` 都教人按 `agents/roles/<role>.md` 取卡，但角色
    token（`ledger` / `story_review` …）**从不等于**卡片文件名（`ledger-editor.md` /
    `story-reviewer.md`）。运行时的 `next` 输出也不带卡片路径，所以总编辑只能照这个模板
    pin 路径——而它对十个角色**全部解析不到**，子代理会拿着一个不存在的路径开工。
    两个方向都要钉住：索引里的每张卡真实存在，且 `agents/roles/` 下每张基础卡都被索引
    （漏登一张＝该角色变成无人能找到的死卡）。
    """
    import re as _re

    roles_md = (REPO / "references" / "roles.md").read_text(encoding="utf-8")
    indexed = set(_re.findall(r"`(agents/roles/[a-z0-9.-]+\.md)`", roles_md))
    assert indexed, "roles.md 里没有任何 `agents/roles/<file>.md` 索引，token→卡片映射断了"
    missing = sorted(rel for rel in indexed if not (REPO / rel).is_file())
    assert not missing, f"roles.md 索引指向不存在的角色卡（总编辑会 pin 到空路径）：{missing}"

    on_disk = {
        f"agents/roles/{p.name}"
        for p in (REPO / "agents" / "roles").glob("*.md")
        if "." not in p.name[: -len(".md")]  # 排除 creative-editor.advanced.md 这类补充文档
    }
    orphans = sorted(on_disk - indexed)
    assert not orphans, f"这些角色卡没被 roles.md 索引，会变成没人找得到的死卡：{orphans}"

    # 占位模板不许复活：它看起来像通用指路，实则对每个 token 都解析失败。
    for rel in ("SKILL.md", "references/roles.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "agents/roles/<role>.md" not in text, (
            f"{rel} 又出现了 `agents/roles/<role>.md` 占位模板：token 与文件名不同名，"
            "这条路径解析不到任何卡片，改用 roles.md 的索引表"
        )




def test_shipped_surface_has_no_unregistered_hooks():
    """分发面不允许出现没有宿主清单注册的钩子配置（"测了但没人调"是假绿）。

    历史缺陷：`hooks/` 里的脚本与 schema 都对、行为测试也全绿，但仓库没有任何宿主清单
    引用它——作为 skill 目录安装时没有宿主会去读，而 `SKILL.md` 却承诺"压实后自动注入
    状态"。测试绿的是脚本，不是那个承诺。

    修法：钩子降级为维护者打包资产（`.dev/plugin-hooks/`），分发面保持干净。
    本用例是反向守卫：谁要把 `hooks/` 加回分发面，必须同时给出宿主清单。
    """
    shipped = REPO / "hooks"
    if not shipped.exists():
        return
    manifests = [
        REPO / ".claude-plugin" / "plugin.json",
        REPO / ".cursor-plugin" / "plugin.json",
        REPO / ".codex-plugin" / "plugin.json",
    ]
    wired = [p for p in manifests if p.is_file() and "hooks" in p.read_text(encoding="utf-8")]
    assert wired, (
        "分发面出现了 hooks/ 但没有任何宿主清单引用它：宿主不会加载，行为测试再绿也是假绿。"
        "要么按插件形态补齐清单，要么把钩子放回 .dev/plugin-hooks/（维护者打包资产）。"
    )






# agent 实际会顺着链接走下去的文档面。.dev/reports/ 是历史归档，
# 里面的相对路径是按仓库根写的（不是按报告所在目录），不在维护范围内。
_LINKED_DOC_GLOBS = ("SKILL.md", "references/**/*.md", "agents/**/*.md", "templates/**/*.md")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")


def test_agent_facing_doc_links_resolve():
    """agent 读得到的文档之间的相对链接必须都能落地。

    渐进式披露靠链接导航：一张角色卡里的 `../../references/roles.md` 写错一层，
    子代理就读到不存在的东西——而它不会报错，只会照着自己已有的印象写下去。
    覆盖 SKILL.md / references / agents / templates 四处的 .md 互链。

    模板目录也要扫：`templates/*.example.md` 是派发给创意/策划编辑的填写范例，
    里面同样有指向 references 的链接；不扫的话 `../craft/...`（少一层）这类死链
    会一直躺在范例里没人发现（本轮质检就是这样漏出来的）。
    """
    broken = []
    for pattern in _LINKED_DOC_GLOBS:
        for src in sorted(REPO.glob(pattern)):
            text = src.read_text(encoding="utf-8")
            for rel in _MD_LINK_RE.findall(text):
                if rel.startswith(("http://", "https://", "/")):
                    continue
                if not (src.parent / rel).resolve().is_file():
                    broken.append(f"{src.relative_to(REPO)} -> {rel}")
    assert not broken, "文档互链指向不存在的文件：\n" + "\n".join(broken)


_CLI_SOURCE = REPO / "scripts" / "novel_ledger_core" / "control" / "cli.py"
_DOC_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]{1,30})")
# argparse 自动提供，不写 add_argument；文档里出现属正常。
_AUTO_FLAGS = {"--help"}
# **宿主侧** CLI 的选项：无人值守驱动配置（`driver.argv`）指向的是宿主自己的无头 CLI，
# 不是 novel-ledger CLI，文档必须能写出它真实的名字才教得会接线（现场事故：给
# `dsh --profile headless` 加 `--model` 才把档位落到进程上）。这里逐个登记并配自检，
# 避免它变成随便往里塞的垃圾桶：一旦 novel-ledger CLI 自己注册了同名选项，自检会提醒删除。
_HOST_CLI_FLAGS = {"--model"}


def test_documented_cli_flags_exist():
    """文档里出现的每个 `--flag` 都必须真的在 CLI 里注册过。

    历史缺陷（本轮质检）：文档教人跑 `book migrate --check`，
    而 `book migrate` 只有 `--apply`（dry-run 是默认）——照文档做会直接 `invalid_args` 失败，
    机器又不会替人发现（文档与代码没有共享的守卫）。这条把"文档承诺的命令行"与 argparse
    实际注册的选项对齐；新增/改名/删除 flag 时，漏改任一侧都会红。

    只校验 flag 是否存在于整个 CLI（不做"某子命令下才有"的精确归属）：精确归属要解析
    argparse 树或逐个跑 `--help`，成本高且易误报；而"flag 被整个删掉/拼错"正是真实事故形态，
    全 CLI 集合足以拦住。
    """
    documented = set()
    for pattern in _LINKED_DOC_GLOBS:
        for src in sorted(REPO.glob(pattern)):
            documented |= set(_DOC_FLAG_RE.findall(src.read_text(encoding="utf-8")))
    declared = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', _CLI_SOURCE.read_text(encoding="utf-8")))
    # 豁免表自检：宿主侧选项一旦被 novel-ledger CLI 注册，豁免就该删掉，否则它会掩盖真冲突。
    stale = sorted(_HOST_CLI_FLAGS & declared)
    assert not stale, f"这些 flag 已被 novel-ledger CLI 注册，请从 _HOST_CLI_FLAGS 移除：{stale}"
    missing = sorted(documented - declared - _AUTO_FLAGS - _HOST_CLI_FLAGS)
    assert not missing, (
        "这些 flag 在文档里出现但 CLI 没有注册（照文档执行会 invalid_args）："
        + "；".join(missing)
    )


# 审校 findings 的四级判据与模型三档，是被刻意铺到多份文档里的一套**共享词汇**。
# 声明"哪些文档必须说这套话"，是防止它在一处被改名、其余七处继续用旧词。
_SEVERITY_VOCAB = ("BLOCKER", "WARNING", "NIT", "UNVERIFIABLE")
_SEVERITY_SPEAKERS = (
    "SKILL.md",
    "references/roles.md",
    "references/pipeline-gates.md",
    "references/dispatch.md",
    "agents/roles/managing-editor.md",
)
# 终审编辑只做通读与引用，不判 BLOCKER/WARNING/NIT；但它必须能报"看不见的"
_SEVERITY_PARTIAL = ("agents/roles/final-reviewer.md",)

_TIER_VOCAB = ("最强档", "标准档", "经济档")
_TIER_SPEAKERS = (
    "SKILL.md",
    "references/dispatch.md",
    "agents/roles/managing-editor.md",
)


def test_review_severity_vocabulary_is_consistent():
    """审校分级与模型档位是被铺到多份文档的一套共享词汇，必须全量一致。

    这套词汇改一处就要改八处：总编辑按分级决定放行/返工/记账、机检按标签分配去向、
    审校角色按标签判定 verdict。任何一处被改名或漏写，agent 就会拿到两套说法，
    而它们不会报错——只会照着各自读到的那份执行。
    本次落地时就靠这条检查发现 `managing-editor.md` 漏了「标准档」。
    """
    offenders = []
    for rel in _SEVERITY_SPEAKERS:
        text = (REPO / rel).read_text(encoding="utf-8")
        missing = [tok for tok in _SEVERITY_VOCAB if tok not in text]
        if missing:
            offenders.append(f"{rel} 缺 {'/'.join(missing)}")
    for rel in _SEVERITY_PARTIAL:
        text = (REPO / rel).read_text(encoding="utf-8")
        if "UNVERIFIABLE" not in text:
            offenders.append(f"{rel} 缺 UNVERIFIABLE（通读角色必须能报看不见的判据）")
    assert not offenders, "审校分级词汇不一致：" + "；".join(offenders)

    for rel in _TIER_SPEAKERS:
        text = (REPO / rel).read_text(encoding="utf-8")
        missing = [tok for tok in _TIER_VOCAB if tok not in text]
        if missing:
            offenders.append(f"{rel} 缺 {'/'.join(missing)}")
    assert not offenders, "模型档位词汇不一致：" + "；".join(offenders)

    # 分级与档位各有一份规范定义，其余文档只引用不改写
    roles_md = (REPO / "references" / "roles.md").read_text(encoding="utf-8")
    assert "## 6. 审校 findings 分级与生命周期" in roles_md, "分级规范定义不在 roles.md §6"
    rules_md = (REPO / "references" / "dispatch.md").read_text(encoding="utf-8")
    assert "## 9. 派发模型分级与成本纪律（Model Tiering）" in rules_md, (
        "档位规范定义不在 dispatch.md §9"
    )


def test_makefile_recipes_use_overridable_python():
    """Makefile 配方不得硬编码 `python3`——无该别名的平台（本机即 Windows）会整套跑不起来。

    历史缺陷（合并带入）：维护目标写成 `python3 .dev/mutation_check.py`，而 Windows / 部分
    发行版只有 `python`。`make` 本身是 Unix 工具，但 Git Bash 等环境装了 make 却无 python3，
    于是 `make mutate` / `make audit` / `make pressure` 全部失败，且没人会立刻发现
    （守卫只检查目标名存在，不检查能不能跑）。改为 `$(PYTHON)` 变量并保留 `python3` 缺省，
    与 `.dev/plugin-hooks/session-start` 的 `PYTHON_BIN` 约定一致。
    """
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^PYTHON\s*\?=", makefile, flags=re.MULTILINE), (
        "Makefile 必须用 `PYTHON ?= python3` 定义可覆盖的解释器变量"
    )
    # 配方行（以 tab 开头）里不得出现裸 `python3` 调用；只查"命令以 python3 开头"，
    # 避免误伤 @echo 提示文字里出现的 python3。
    bare = [
        line
        for line in makefile.splitlines()
        if line.startswith("\t")
        and re.match(r"\t@?python3\s", line)
    ]
    assert not bare, "Makefile 配方硬编码 python3（无该别名的平台会失败）：\n" + "\n".join(bare)
    assert "$(PYTHON)" in makefile, "Makefile 配方应通过 $(PYTHON) 调用解释器"




def test_infra_layer_is_not_bypassed():
    """依赖方向必须单向：领域层（content/ledger/voice）→ infra，控制层 → 全部。

    `util.py` 与 `BookStore` 原都在 `control/store.py` 下，于是 `content` / `ledger` /
    `voice` 得从"编排层" import 错误类型、原子写与持久化对象——领域反向依赖编排
    （质检 F-02）。两步拆完之后：`infra/` 只依赖 `infra/util.py`，领域层不许 import
    `control.*`，控制层可以 import 所有层。

    这条守卫防它被下一次"就近 import"搬回去：那是纯移动，只测行为的话测试照样全绿。
    """
    import ast

    core = REPO / "scripts" / "novel_ledger_core"
    assert (core / "infra" / "util.py").is_file(), "通用设施必须住在 infra/ 下"

    def targets(path: Path) -> list[str]:
        pkg = list(path.relative_to(core).parts[:-1])
        found: list[str] = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                found.append(".".join([*base, node.module]))
        return found

    offenders: list[str] = []
    for base in ("content", "ledger", "voice"):
        for path in sorted((core / base).rglob("*.py")):
            for target in targets(path):
                if target == "control" or target.startswith("control."):
                    offenders.append(f"{path.relative_to(REPO)} -> {target}")
    for path in sorted((core / "infra").rglob("*.py")):
        for target in targets(path):
            if target.split(".")[0] in ("control", "content", "ledger", "voice"):
                offenders.append(f"{path.relative_to(REPO)} -> {target}")

    assert not offenders, "基础设施层的依赖方向被破坏：\n" + "\n".join(offenders)


def test_config_has_no_write_only_knobs():
    """配置里不许留"只写不读"的旋钮：接受参数、写进文件、没有任何消费者。

    历史：`init --mode {unattended,assisted}` 被 CLI 接受并写进 `config.mode`，
    但全仓零读取（`assisted` 在任何文档里都没出现），状态机也没有对应分支；
    `skill` / `skill_version` 同样只有定义没有读者，且 `skill_version` 固定 `0.1.0`
    永不更新——它们让配置**看起来**比实际更有控制力，是误导而非能力。

    兼容性由 `schema_version` 单独承担：它有读取守卫（高于当前即停线）与 `book migrate`。
    要加新配置项，先给出读取点，再加进来。
    """
    from novel_ledger_core.infra.store import DEFAULT_CONFIG

    dead = [key for key in ("mode", "skill", "skill_version") if key in DEFAULT_CONFIG]
    assert not dead, f"这些配置项只写不读，别加回来：{dead}"


