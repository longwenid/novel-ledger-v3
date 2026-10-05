from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.content.pack import assemble_pack, inputs_fingerprint
from novel_ledger_core.control.pipeline import chapter_next
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json
from novel_ledger_core.voice.voice_manual import (
    DEFAULT_VOICE_ID,
    iter_voice_manuals,
    parse_skill_md,
    voice_manual_path,
)

BUNDLED_VOICE_IDS = ("shijing", "cinematic", "baimiao", "mystery", "heroic", "lyrical")


def _ends_with_posix(path_like: object, rel: str) -> bool:
    """判断 OS 原生路径是否以给定的 POSIX 相对路径结尾。

    `skill_manual` / `voice_skill_manual` 是 `str(Path(...))`，Windows 上分隔符是
    反斜杠；直接 `.endswith("references/voices/x.md")` 会误判失败。归一化后再比。
    """
    return str(path_like or "").replace("\\", "/").endswith(rel)


def _core_concept(concepts: list[str]) -> str:
    """选一条场景原则，验证长驻概念不会被会话笔记挤掉。"""
    for item in concepts:
        if "当前视角" in item:
            return item
    raise AssertionError(f"没有找到视角概念: {concepts}")


def test_parse_shijing_skill_md():
    meta = parse_skill_md(voice_manual_path("shijing"))
    assert "限制视角" in meta["style_formula"]
    assert 5 <= len(meta["checklist"]) <= 8
    assert 8 <= len(meta["concepts"]) <= 12
    joined = " ".join(meta["concepts"] + meta["checklist"])
    assert "对白" in joined and "关系" in joined and "当前视角" in joined
    assert "附件短摘" in meta["concepts"][0]


def test_bundled_voice_manuals_share_parse_contract():
    """六种文风共用解析契约；随包只保留总手册和草稿内容手册。"""
    for voice_id in BUNDLED_VOICE_IDS:
        path = voice_manual_path(voice_id)
        text = path.read_text(encoding="utf-8")
        meta = parse_skill_md(path)
        assert meta["style_formula"], voice_id
        assert 8 <= len(meta["concepts"]) <= 21, voice_id
        assert len(meta["checklist"]) >= 5, voice_id
        assert "### 硬禁" in text, voice_id
        assert all(item in text for item in meta["concepts"]), voice_id
        assert all(item in text for item in meta["checklist"]), voice_id
        content = path.with_name(f"{voice_id}.content.md")
        assert content.is_file() and content.read_text(encoding="utf-8").strip(), content
        assert not path.with_name(f"{voice_id}.writer.md").exists(), voice_id


def test_external_voice_can_still_use_optional_writer_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """外部文风仍可用独立 writer；缺省时润色直接读取总手册。"""
    from novel_ledger_core.voice import voice_manual as vm

    manual = tmp_path / "external.md"
    writer = tmp_path / "external.writer.md"
    manual.write_text("总手册", encoding="utf-8")
    monkeypatch.setattr(vm, "voice_manual_path", lambda _voice_id: manual)

    assert vm.load_writing_manual("external") == (str(manual), "总手册")
    writer.write_text("独立润色手册", encoding="utf-8")
    assert vm.load_writing_manual("external") == (str(writer), "独立润色手册")


def test_parse_skill_md_checklist_stops_at_next_top_heading():
    """回归：自检清单的终止条件曾是「下一个 `##`」，于是一路吞掉分隔线和下一个一级标题，
    把 `---`、`# 《×××》文风分析` 及其引用块（8 行垃圾）一并抓进 checklist，
    只靠 `[:11]` 这个跟手册条数耦合的魔数侥幸没暴露——手册里增删一条自检就会漏进 pack。"""
    checklist = parse_skill_md(voice_manual_path("shijing"))["checklist"]
    assert checklist
    for item in checklist:
        assert not item.startswith("---"), item
        assert not item.startswith("#"), item
        assert not item.startswith(">"), item
    # 每条都必须是原文里「数字编号 + 点号」开头的行（结构化判据，不是魔数截断）
    manual = voice_manual_path("shijing").read_text(encoding="utf-8")
    for i, item in enumerate(checklist, start=1):
        assert f"{i}. {item}" in manual, item
    assert "全部通过才算完成" not in " ".join(checklist)


def test_parse_skill_md_concepts_have_no_duplicates():
    """回归：概念小节正则没做行首锚定，`##\\s*\\d+\\.` 会命中三级标题 `### 1. …` 的后两个
    `#`（当年手册文末内联了一份带 `### N.` 小节的分析报告，同一语义被抓两遍，白占三分之一
    滑窗）。报告已移出到 `.dev/wenfeng-analysis.md`，行首锚定保留下来防下一次。

    顺带钉住结构判据：每条概念都必须是手册里某个小节的 `- ` 条目原文，不是正则跨节粘出来的。
    """
    manual = voice_manual_path("shijing").read_text(encoding="utf-8")
    concepts = parse_skill_md(voice_manual_path("shijing"))["concepts"]
    assert len(concepts) == len(set(concepts)), concepts
    for item in concepts:
        assert f"- {item}" in manual or f"- **硬禁**：{item}" in manual, item


def test_parse_skill_md_uses_style_agnostic_headings(tmp_path: Path):
    """多套文风共用解析器：公式 / 编号小节 / `### 硬禁` / `## 自检`，不跟某套手册的编号耦合。"""
    path = tmp_path / "demo.md"
    path.write_text(
        "# 演示\n\n"
        "> **公式甲**\n\n"
        "## 1. 第一则\n- 概念一\n\n"
        "### 硬禁\n- **硬禁**：硬规则甲\n- 硬规则乙\n\n"
        "## 2. 第二则\n- 概念二\n\n"
        "## 8. 交付前自检清单\n\n1. 旧清单不该被抓\n\n"
        "## 自检\n\n1. 检查甲\n2. 检查乙\n",
        encoding="utf-8",
    )
    meta = parse_skill_md(path)
    assert meta["style_formula"] == "公式甲"
    assert meta["concepts"][0] == "硬规则甲"
    assert meta["concepts"][1] == "硬规则乙"
    assert "概念一" in meta["concepts"]
    assert "概念二" in meta["concepts"]
    assert meta["checklist"] == ["检查甲", "检查乙"]


def _init_voice_project(tmp_path: Path, *, voice: str | None = None) -> Path:
    project = tmp_path / "novel"
    plan = {
        "protagonist": "甲",
        "volume_spine": "试写",
        "chapters": [
            {
                "chapter": 1,
                "location": "院中",
                "present": ["乙"],
                "beats": [{"id": "b1", "required": True, "text": "压力", "must": "压力"}],
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    init_project(project, plan_path=plan_path, voice=voice)
    return project


def test_each_bundled_voice_reaches_the_correct_pack_layers(tmp_path: Path):
    """选择任一内置文风，草稿读 content，润色读总手册。"""
    for voice_id in BUNDLED_VOICE_IDS:
        voice_root = tmp_path / voice_id
        voice_root.mkdir()
        project = _init_voice_project(voice_root, voice=voice_id)
        store = BookStore(project)
        pack = assemble_pack(store, 1)
        path = voice_manual_path(voice_id)
        assert store.load_voice_profile()["voice_id"] == voice_id
        expected_content = path.with_name(f"{voice_id}.content.md").read_text(encoding="utf-8")
        expected_writing = path.read_text(encoding="utf-8")
        assert pack["voice_content_text"].strip() == expected_content.strip()
        assert pack["voice_writing_text"] == expected_writing
        assert pack["voice_writing_manual"] == str(path)
        assert "voice_manual_text" not in pack
        assert pack["style_formula"] == parse_skill_md(path)["style_formula"]


def test_voice_apply_and_pack_injection(tmp_path: Path):
    project = _init_voice_project(tmp_path)

    code = main(
        [
            "voice",
            "apply",
            "--project",
            str(project),
            "--cap",
            "12",
        ]
    )
    assert code == 0
    voice = read_json(project / "book/memory/voice.json")
    assert voice.get("voice_id") == "shijing"
    assert voice.get("skill_manual")
    assert _ends_with_posix(voice["skill_manual"], "references/voices/shijing.md")
    assert voice.get("style_formula")
    assert 5 <= len(voice.get("checklist") or []) <= 8
    voice_joined = " ".join(voice.get("concepts") or []) + " ".join(voice.get("checklist") or [])
    assert "当前视角" in voice_joined and "对白" in voice_joined
    assert "附件短摘" in voice["concepts"][0]

    store = BookStore(project)
    pack = assemble_pack(store, 1)
    assert pack["voice_concepts"]
    assert pack.get("style_formula")
    assert pack.get("voice_checklist") == voice["checklist"]
    # instruction 只放短指引；总手册内文在 voice_writing_text，自检不再重复成【自检】清单。
    assert _ends_with_posix(pack["voice_skill_manual"], "references/voices/shijing.md")
    # 总手册是润色正文真源；同一内文只以 voice_writing_text 进入 canonical。
    assert "voice_manual_text" not in pack
    assert pack["voice_writing_text"]
    assert pack["voice_writing_text"] == voice_manual_path("shijing").read_text(encoding="utf-8")
    assert len(pack["voice_writing_text"]) > 1000
    assert pack["voice_writing_text"] not in pack["instruction"]
    assert "voice_writing_text" in pack["instruction"]
    assert pack["style_formula"] == voice["style_formula"]
    assert pack["instruction"].count("【自检】") == 0


def test_canonical_manual_used_for_polish_view(tmp_path: Path):
    project = _init_voice_project(tmp_path, voice="cinematic")
    store = BookStore(project)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    store.save_config(cfg)
    chapter_next(store)
    canon = read_json(store.current_pack_path)
    polish = read_json(store.polish_pack_path(1))
    assert "voice_writing_text" in canon
    # 润色视图：总手册内文，键名与 canonical pack 同名同义（voice_writing_text）。
    assert polish["voice_writing_text"] == canon["voice_writing_text"]
    # 总手册不再另以 voice_manual_text 重复入包。
    from novel_ledger_core.voice.voice_manual import load_writing_manual

    write_path, write_text = load_writing_manual("cinematic")
    assert "voice_manual_text" not in canon
    assert polish["voice_writing_text"] == write_text
    assert polish["voice_writing_manual"] == canon["voice_writing_manual"] == write_path
    assert write_path == str(voice_manual_path("cinematic"))


def test_voice_profile_has_no_quantitative_fields(tmp_path: Path):
    """量化那一半已删：profile 里不许再出现基线字段。

    留着空的 `baseline_metrics` / `baseline_stats` 会让读者以为还有个打分口径在别处生效，
    也会白进 `inputs_fingerprint`（voice profile 整体参与指纹）。
    """
    project = _init_voice_project(tmp_path)
    assert main(["voice", "apply", "--project", str(project)]) == 0
    voice = read_json(project / "book/memory/voice.json")
    for dead in ("baseline_metrics", "baseline_stats", "distill_path", "skill_analysis", "distilled"):
        assert dead not in voice, dead
    # 手册 → pack 这条线的字段必须齐
    for key in ("voice_id", "concepts", "checklist", "style_formula", "skill_manual", "concept_cap"):
        assert key in voice, key


def test_session_notes_never_evict_distilled_concepts(tmp_path: Path):
    """会话笔记只占剩余槽，不挤掉手册中的场景原则。"""
    from novel_ledger_core.control.pipeline import _append_voice_concept

    project = _init_voice_project(tmp_path)
    assert main(["voice", "apply", "--project", str(project)]) == 0

    store = BookStore(project)
    applied = store.load_voice_concepts()
    core = _core_concept(applied)
    first_concept = assemble_pack(store, 1)["voice_concepts"][0]
    assert first_concept in applied

    for i in range(10):
        _append_voice_concept(store, f"临时笔记第{i}条：灶台上的水开了就先端下来")
        # 长驻窗口逐字不动，笔记全进独立窗口
        assert store.load_voice_concepts() == applied, i
        assert store.load_voice_session_notes(), i
        concepts = assemble_pack(store, 1)["voice_concepts"]
        assert core in concepts, i
        assert set(applied) <= set(concepts), i
        assert store.load_voice_session_notes()[-1] in concepts, i


def test_session_notes_remain_complete_beyond_legacy_fifo_setting(tmp_path: Path):
    """The old FIFO setting must not silently erase persistent author feedback."""
    from novel_ledger_core.control.pipeline import _append_voice_concept

    project = _init_voice_project(tmp_path)
    assert main(["voice", "apply", "--project", str(project)]) == 0
    store = BookStore(project)
    cfg = store.load_config()
    cfg["voice_session_notes_cap"] = 6  # compatibility with old projects
    store.save_config(cfg)
    cap = 6

    for i in range(cap + 3):
        _append_voice_concept(store, f"笔记{i}")
    notes = store.load_voice_session_notes()
    assert notes == [f"笔记{i}" for i in range(cap + 3)]
    assert set(notes) <= set(assemble_pack(store, 1)["voice_concepts"])
    # 「不要」「禁止」开头仍然直接丢弃（"不建禁词表"的代码落点）
    _append_voice_concept(store, "不要写说明书腔")
    _append_voice_concept(store, "禁止对白开会")
    assert store.load_voice_session_notes() == notes


def test_session_notes_are_not_dropped_when_manual_exhausts_legacy_slots(tmp_path: Path):
    """Selected manual concepts and existing feedback are both retained in full."""
    from novel_ledger_core.control.pipeline import _append_voice_concept

    project = _init_voice_project(tmp_path)
    assert main(["voice", "apply", "--project", str(project)]) == 0
    store = BookStore(project)
    cfg = store.load_config()
    cfg["pack_caps"]["voice_concepts"] = len(store.load_voice_concepts()) + 2
    store.save_config(cfg)

    for i in range(4):
        _append_voice_concept(store, f"笔记{i}：数字要精确到几文钱")
    concepts = assemble_pack(store, 1)["voice_concepts"]
    assert _core_concept(concepts)
    assert concepts[-4:] == [f"笔记{i}：数字要精确到几文钱" for i in range(4)]


def test_voice_json_must_be_object(tmp_path: Path):
    """voice.json 必须是对象。列表形式的旧文件直接 invalid_voice，不做迁移。"""
    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    store.voice_memory_path.write_text(json.dumps(["一条概念"]), encoding="utf-8")
    with pytest.raises(LedgerError) as exc:
        store.load_voice_profile()
    assert exc.value.code == "invalid_voice"


def test_parse_skill_md_does_not_fallback_to_formula_as_concept(tmp_path: Path):
    """公式不能顶替概念：抽不到 numbered 小节时 concepts 必须空，交给 invalid_voice_manual。"""
    path = tmp_path / "voice.md"
    path.write_text(
        "# 手册\n\n> **公式还在**\n\n## 自检\n\n1. 有对话吗？\n",
        encoding="utf-8",
    )
    meta = parse_skill_md(path)
    assert meta["style_formula"] == "公式还在"
    assert meta["checklist"] == ["有对话吗？"]
    assert meta["concepts"] == []


def test_init_applies_default_shijing_without_voice_apply(tmp_path: Path):
    """文风单轨：`init` 不传 --voice 时蒸馏 shijing，写进 voice profile。

    `voice apply` 只是手册改动后的刷新入口；任何一个项目都不可能在没有文风概念的状态下存在。
    """
    manual = parse_skill_md(voice_manual_path("shijing"))
    project = _init_voice_project(tmp_path)
    store = BookStore(project)

    profile = store.load_voice_profile()
    assert "distilled" not in profile
    assert profile["voice_id"] == DEFAULT_VOICE_ID == "shijing"
    assert _ends_with_posix(profile["skill_manual"], "references/voices/shijing.md")
    # 精简手册的概念须全部进入 profile。
    assert 8 <= len(manual["concepts"]) <= 12
    assert len(profile["concepts"]) == len(manual["concepts"])
    # 验证 profile 的概念就是手册的概念
    assert profile["concepts"] == manual["concepts"]
    assert profile["checklist"] == manual["checklist"]

    # 不跑 voice apply，直接就能开章，且公式/自检/手册路径都在 pack 里
    pack = assemble_pack(store, 1)
    # pack 的概念上限留有余量，手册概念全部进入。
    assert len(pack["voice_concepts"]) == len(manual["concepts"])
    assert pack["voice_concepts"] == manual["concepts"]
    # 硬禁首条应该在其中
    assert pack["voice_concepts"][0] in manual["concepts"]
    assert pack["style_formula"] == manual["style_formula"]
    assert pack["voice_checklist"]
    assert _ends_with_posix(pack["voice_skill_manual"], "references/voices/shijing.md")
    # 总手册以润色阶段字段进入 canonical，不重复放在旧字段里。
    assert "voice_manual_text" not in pack
    assert pack["voice_writing_text"]
    assert pack["voice_writing_manual"] == str(voice_manual_path("shijing"))
    assert pack["voice_writing_text"] not in pack["instruction"]


def test_pack_rereads_live_manual_ignoring_stale_skill_manual_path(tmp_path: Path):
    """voice.json 冻结的绝对路径过期时，pack 仍从当前 skill_root 注入全文。

    历史事故：init 时 skill 在 AndroidStudioProjects/...，后来仓库搬到 AiGit/novel，
    写子 agent 按 pack.voice_skill_manual 去开那份旧路径。
    """
    from novel_ledger_core.voice.voice_manual import voice_manual_path

    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    profile = store.load_voice_profile()
    stale = "/tmp/stale-voice-clone/references/voices/shijing.md"
    profile["skill_manual"] = stale
    store.save_voice_profile(profile)

    pack = assemble_pack(store, 1)
    live = str(voice_manual_path("shijing"))
    assert pack["voice_skill_manual"] == live
    assert pack["voice_skill_manual"] != stale
    assert stale not in pack["instruction"]
    # 手册内容仍从当前 skill_root 注入润色阶段字段。
    from novel_ledger_core.voice.voice_manual import load_writing_manual

    write_path, write_text = load_writing_manual("shijing")
    assert pack["voice_writing_text"] == write_text
    assert pack["voice_writing_manual"] == write_path == live
    assert "voice_manual_text" not in pack
    assert pack["write_contract"]["gates"][0].startswith("只从终稿正文提取账本字段")
    assert "chapter submit" in " ".join(pack["write_contract"]["gates"])


def test_skill_root_keeps_import_path_not_symlink_target():
    """`.cursor/skills/novel-ledger` 若是 symlink，对外路径停在入口，不跟进 resolve 目标。"""
    from novel_ledger_core.voice import voice_manual as vm

    imported = Path(vm.__file__).absolute().parents[3]
    resolved = Path(vm.__file__).resolve().parents[3]
    assert vm.skill_root() == imported
    live = str(vm.voice_manual_path("shijing"))
    assert live.startswith(str(imported))
    if imported != resolved:
        assert not live.startswith(str(resolved))


def test_cli_entry_does_not_resolve_scripts_dir_into_sys_path():
    """入口若 resolve() scripts 目录，skill_root 会跟进 symlink，ack 拿到另一棵树的手册路径。"""
    entry = Path(__file__).parent.parent / "novel_ledger.py"
    text = entry.read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parent" not in text
    assert "sys.path.insert(0, str(Path(__file__).parent))" in text


def test_init_voice_cinematic_pack_uses_cinematic_manual(tmp_path: Path):
    manual = parse_skill_md(voice_manual_path("cinematic"))
    project = _init_voice_project(tmp_path, voice="cinematic")
    store = BookStore(project)
    profile = store.load_voice_profile()
    assert profile["voice_id"] == "cinematic"
    assert _ends_with_posix(profile["skill_manual"], "references/voices/cinematic.md")
    assert profile["concepts"] == manual["concepts"]
    assert profile["style_formula"] == manual["style_formula"]
    pack = assemble_pack(store, 1)
    assert _ends_with_posix(pack["voice_skill_manual"], "references/voices/cinematic.md")
    # 总手册直接进入润色阶段字段，不再另存 voice_manual_text。
    assert "voice_manual_text" not in pack
    assert pack["voice_writing_text"]
    assert pack["voice_writing_manual"] == str(voice_manual_path("cinematic"))
    assert pack["voice_writing_text"] not in pack["instruction"]
    assert pack["voice_concepts"] == manual["concepts"]
    assert pack["style_formula"] == manual["style_formula"]
    assert "限制视角" not in pack["style_formula"]


def test_init_unknown_voice_leaves_no_project(tmp_path: Path):
    project = tmp_path / "novel"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"protagonist": "甲", "chapters": []}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(LedgerError) as exc:
        init_project(project, plan_path=plan_path, voice="no-such")
    assert exc.value.code == "unknown_voice"
    assert not (project / "book").exists()
    for name in ("config.json", "HEAD.json"):
        assert not (project / "book" / name).exists()


def test_init_invalid_voice_manual_leaves_no_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """手册解析空走 CLI 时，写锁还没建 book/run/ 就必须失败。"""
    monkeypatch.setattr(
        "novel_ledger_core.voice.voice_manual.parse_skill_md",
        lambda _path: {"style_formula": "公式还在", "checklist": [], "concepts": []},
    )
    project = tmp_path / "novel"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"protagonist": "甲", "chapters": []}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(LedgerError) as exc:
        init_project(project, plan_path=plan_path)
    assert exc.value.code == "invalid_voice_manual"
    assert not (project / "book").exists()


def test_build_voice_profile_rejects_empty_formula(monkeypatch: pytest.MonkeyPatch):
    from novel_ledger_core.voice.voice_manual import build_voice_profile

    monkeypatch.setattr(
        "novel_ledger_core.voice.voice_manual.parse_skill_md",
        lambda _path: {"style_formula": "", "checklist": ["检查甲"], "concepts": ["概念甲"]},
    )
    with pytest.raises(LedgerError) as exc:
        build_voice_profile(voice_id="shijing", pack_cap=14)
    assert exc.value.code == "invalid_voice_manual"
    assert exc.value.details["formula"] is False


def test_writer_memory_old_concepts_key_is_ignored(tmp_path: Path):
    """写者只能走 memory.voice_concepts；旧键 concepts 不做迁移兼容。"""
    from novel_ledger_core.control.pipeline import _merge_voice_memory

    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    before = store.load_voice_session_notes()
    _merge_voice_memory(store, {"concepts": ["旧键不该进笔记"]})
    assert store.load_voice_session_notes() == before
    _merge_voice_memory(store, {"voice_concepts": ["新键该进笔记"]})
    assert "新键该进笔记" in store.load_voice_session_notes()


def test_init_fails_hard_when_manual_parses_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """手册解析不出概念时 `init` 当场失败，且不留下半个项目。

    写出一个没有文风概念的项目，第一次 `chapter next` 才会以 missing_voice 挂掉，
    而那时 HEAD/账本/章拍都已落盘。所以校验放在任何写盘之前。
    """
    monkeypatch.setattr(
        "novel_ledger_core.voice.voice_manual.parse_skill_md",
        lambda _path: {"style_formula": "公式还在", "checklist": [], "concepts": []},
    )
    project = tmp_path / "novel"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"protagonist": "甲", "chapters": []}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(LedgerError) as exc:
        init_project(project, plan_path=plan_path)
    assert exc.value.code == "invalid_voice_manual"
    assert "shijing.md" in exc.value.message

    store = BookStore(project)
    for path in (store.config_path, store.head_path, store.plan_path, store.voice_memory_path):
        assert not path.exists(), path


def test_pack_refuses_chapter_without_voice_concepts(tmp_path: Path):
    """最后一道防线：voice.json 被清空/改坏时 pack 拒绝开章。

    missing_voice 是"写者裸奔"与"停线"之间唯一的闸。
    """
    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    store.save_voice_profile({"schema": "novel-ledger.voice-memory.v1", "concepts": []})
    with pytest.raises(LedgerError) as exc:
        assemble_pack(store, 1)
    assert exc.value.code == "missing_voice"
    assert "voice apply" in exc.value.message


def test_pack_cap_leaves_room_for_session_notes():
    """pack cap 必须比手册蒸馏出的概念条数**多出几槽**给会话笔记。

    概念优先占槽，cap 一旦贴着概念条数，ack 的 `--voice-note` 就静默归零：笔记照样落盘、
    照样 FIFO，只是一条也进不了 pack，而这件事没有任何告警。手册每加一个带编号的小节
    概念就多一条，所以这条守卫盯的是"余量"，不是某个具体数字。每套随包文风都要过。
    """
    from novel_ledger_core.infra.store import DEFAULT_CONFIG

    cap = int(DEFAULT_CONFIG["pack_caps"]["voice_concepts"])
    manuals = iter_voice_manuals()
    assert manuals
    for voice_id, path in manuals:
        concepts = len(parse_skill_md(path)["concepts"])
        assert cap - concepts >= 3, (
            f"{voice_id}: pack cap {cap} 只给笔记留了 {cap - concepts} 槽（手册 {concepts} 条概念）"
        )


def test_voice_apply_retains_complete_manual_despite_legacy_caps(tmp_path: Path, capsys):
    """Old apply and pack caps cannot delete selected manual concepts."""
    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    pack_cap = int(store.load_config()["pack_caps"]["voice_concepts"])
    assert pack_cap == 24  # 当前 pack_cap

    expected = parse_skill_md(voice_manual_path("shijing"))["concepts"]
    assert main(["voice", "apply", "--project", str(project), "--cap", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pack_concept_cap"] == pack_cap
    assert payload["concept_cap_requested"] == 1
    assert payload["concept_cap"] == len(expected)
    assert payload["concept_count"] == len(expected)
    assert payload["voice_id"] == "shijing"

    # 存进 voice.json 的每一条都必须在 pack 里真的到得了写者
    stored = store.load_voice_concepts()
    assert stored == expected

    # A larger legacy setting produces the same full material.
    assert main(["voice", "apply", "--project", str(project), "--cap", "25"]) == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["concept_cap_requested"] == 25
    assert payload2["concept_cap"] == len(expected)
    stored2 = store.load_voice_concepts()
    assert stored2 == expected
    assert set(stored) <= set(assemble_pack(store, 1)["voice_concepts"])

    # 不给 --cap 时默认即 pack cap
    assert main(["voice", "apply", "--project", str(project)]) == 0
    default_payload = json.loads(capsys.readouterr().out)
    assert default_payload["concept_cap"] == len(expected)
    assert default_payload["concept_cap_requested"] == pack_cap


def test_voice_apply_preserves_session_notes(tmp_path: Path):
    """重跑 apply 不该静默清空另一条数据流累积的会话笔记。"""
    from novel_ledger_core.control.pipeline import _append_voice_concept

    project = _init_voice_project(tmp_path)
    assert main(["voice", "apply", "--project", str(project)]) == 0
    store = BookStore(project)
    _append_voice_concept(store, "笔记：灶台上的水开了")
    assert main(["voice", "apply", "--project", str(project)]) == 0
    assert store.load_voice_session_notes() == ["笔记：灶台上的水开了"]


def test_voice_apply_switches_to_cinematic_and_keeps_session_notes(tmp_path: Path):
    from novel_ledger_core.control.pipeline import _append_voice_concept

    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    before_fp = inputs_fingerprint(store, 1)
    _append_voice_concept(store, "笔记：灶台上的水开了")
    assert main(["voice", "apply", "--project", str(project), "--voice", "cinematic"]) == 0
    profile = store.load_voice_profile()
    cinematic = parse_skill_md(voice_manual_path("cinematic"))
    assert profile["voice_id"] == "cinematic"
    assert _ends_with_posix(profile["skill_manual"], "references/voices/cinematic.md")
    assert profile["concepts"] == cinematic["concepts"]
    assert store.load_voice_session_notes() == ["笔记：灶台上的水开了"]
    pack = assemble_pack(store, 1)
    assert pack["voice_skill_manual"].endswith("cinematic.md")
    assert pack["voice_concepts"][0] == cinematic["concepts"][0]
    assert pack["style_formula"] == cinematic["style_formula"]
    assert inputs_fingerprint(store, 1) != before_fp


def test_voice_apply_refuses_empty_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """回归：解析空时 apply 曾照样写空 `concepts`，管线继续跑、文风静默失效、零告警。
    现在必须当场报错。

    单轨之后后果更重：拒绝写入还必须**不动**已有 profile——把 init 注入的概念
    覆盖成空，等于让这个项目从此开不了章（pack 会以 missing_voice 拒绝）。
    """
    project = _init_voice_project(tmp_path)
    before = BookStore(project).voice_memory_path.read_bytes()
    monkeypatch.setattr(
        "novel_ledger_core.voice.voice_manual.parse_skill_md",
        lambda _path: {"style_formula": "公式还在", "checklist": [], "concepts": []},
    )
    code = main(["voice", "apply", "--project", str(project)])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_voice_manual"
    assert "shijing.md" in payload["error"]["message"]
    # 拒绝写入：init 注入的文风逐字保留，项目仍然开得了章
    assert BookStore(project).voice_memory_path.read_bytes() == before
    assert assemble_pack(BookStore(project), 1)["voice_concepts"]


def test_voice_list_includes_bundled_manuals(capsys):
    code = main(["voice", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    ids = {item["id"] for item in payload["voices"]}
    assert set(BUNDLED_VOICE_IDS) <= ids
    titles = {item["id"]: item["title"] for item in payload["voices"]}
    assert all(titles[voice_id] for voice_id in BUNDLED_VOICE_IDS)
    assert len({titles[voice_id] for voice_id in BUNDLED_VOICE_IDS}) == len(BUNDLED_VOICE_IDS)


def test_removed_quant_subcommands_are_gone(capsys):
    """`voice distill` / `voice score` 已删：CLI 必须以 invalid_args 拒绝，不能静默变成别的命令。"""
    for argv in (["voice", "distill", "--source", "x", "--out", "y"], ["voice", "score", "--prose", "x"]):
        code = main(argv)
        payload = json.loads(capsys.readouterr().out)
        assert code != 0, argv
        assert payload["ok"] is False, argv
        assert payload["error"]["code"] == "invalid_args", argv


def test_inputs_fingerprint_covers_layered_manuals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """两份内置手册是 draft/polish 视图的上游真源，必须参与 inputs_fingerprint。

    盲区：指纹只含 voice profile（总手册的解析产物），章中改 `<id>.md` 或
    `<id>.content.md` 原文不触发 stale 重建，而重建后的 polish/draft 视图已经变了。
    外部文风可另有 writer companion，因而三个加载入口都应参与指纹。
    """
    from novel_ledger_core.content import pack as pack_mod

    project = _init_voice_project(tmp_path)
    store = BookStore(project)
    before = inputs_fingerprint(store, 1)
    for loader_name, added_text in (
        ("load_live_manual", "新增总手册润色说明。"),
        ("load_writing_manual", "新增外部写作层禁词。"),
        ("load_content_manual", "新增内容层心智模型。"),
    ):
        original = getattr(pack_mod, loader_name)
        with monkeypatch.context() as patch:
            patch.setattr(
                pack_mod,
                loader_name,
                lambda vid: ("fake.md", original(vid)[1] + "\n" + added_text),
            )
            assert inputs_fingerprint(store, 1) != before, f"{loader_name} 变化未使 pack 指纹失配"
