from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.content.extract import DIR_KIND_MAP, extract_cards, extract_to_file
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.util import LedgerError, read_json


def test_extract_hard_paragraphs_not_whole_file(tmp_path: Path):
    src = tmp_path / "kb"
    src.mkdir()
    (src / "world.md").write_text(
        "\n".join(
            [
                "# 世界观",
                "",
                "【软】" + "这一大段软文重复很多遍。" * 40,
                "",
                "## 地理",
                "",
                "【硬】石桥从北坡入城，涨潮时桥柱没过膝。",
                "",
                "【软】可以写成黄昏很美，适合谈心。",
                "",
                "## 力量规则",
                "",
                "【硬】规则三层，越界没有豁免。" + "完整铁律证据。" * 100 + "硬规则尾部标记",
                "",
                "后面还有很长的闲笔。" * 50,
            ]
        ),
        encoding="utf-8",
    )
    roster_dir = src / "人物"
    roster_dir.mkdir()
    (roster_dir / "名录.md").write_text(
        "# 人物名录\n\n- 主角：码头伙计\n- 掌柜：管凭据\n",
        encoding="utf-8",
    )
    out = tmp_path / "cards.json"
    payload = extract_to_file(src, out, excerpt_max=80)
    assert payload["ok"] is True
    data = read_json(out)
    assert "cards" in data
    assert payload["skipped_roster"] >= 1
    bodies = [c["body"] for c in data["cards"]]
    titles = [c["title"] for c in data["cards"]]
    joined = "\n".join(bodies)
    assert "石桥从北坡入城" in joined
    assert "规则三层" in joined
    assert "人物名录" not in titles
    assert "完整铁律证据。" * 100 + "硬规则尾部标记" in joined
    assert "这一大段软文重复很多遍。" * 40 not in joined
    mega = "".join(bodies)
    raw = (src / "world.md").read_text(encoding="utf-8")
    assert len(mega) < len(raw)


def test_extract_missing_source_errors(tmp_path: Path):
    missing = tmp_path / "no-such-kb"
    with pytest.raises(LedgerError) as exc:
        extract_cards(missing)
    assert exc.value.code == "missing_source"


def test_extract_cli_writes_cards(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "rule.md").write_text(
        "## 开场规则\n\n【硬】开门不带行囊，只能用这具身体会的东西。\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    code = main(["kb", "extract", "--source", str(src), "--out", str(out), "--excerpt-max", "120"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["cards"] >= 1
    assert read_json(out)["cards"][0]["body"]


def test_kb_sync_from_canon_keeps_full_table(tmp_path: Path, capsys):
    proj = tmp_path / "bookproj"
    from novel_ledger_core.control.bootstrap import init_project
    init_project(proj, protagonist="主角")
    canon = tmp_path / "canon-source"
    canon.mkdir(parents=True)
    (canon / "层级.md").write_text(
        "\n".join(
            [
                "硬度: 硬",
                "",
                "# 层级与上限",
                "",
                "### 【硬】层级上限表",
                "",
                "| 层级 | 上限 |",
                "|---|---|",
                "| 一阶 | 60–100 |",
                "| 二阶 | 200–250 |",
                "| 三阶 | 500–600 |",
                "| 四阶 | 800–1200 |",
                "| 五阶 | 2000–3000 |",
            ]
        ),
        encoding="utf-8",
    )
    (canon / "README.md").write_text("# 索引\n\n人读正典。\n", encoding="utf-8")
    code = main(["database", "import-source", "--project", str(proj), "--kind", "canon", "--source", str(canon)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    cards = read_json(proj / "book" / "kb" / "cards.json")["cards"]
    joined = "\n".join(c["body"] for c in cards)
    assert "2000–3000" in joined
    assert "800–1200" in joined
    assert "人读正典" not in joined


def test_extract_kernel_has_no_book_instance_dirs():
    assert "广成界" not in DIR_KIND_MAP
    assert "仙凡秩序" not in DIR_KIND_MAP
    assert "资源百艺" not in DIR_KIND_MAP


def test_extract_still_reads_book_named_folder(tmp_path: Path):
    src = tmp_path / "kb" / "本地设定库"
    src.mkdir(parents=True)
    (src / "rule.md").write_text(
        "## 力量规则\n\n【硬】越界没有豁免，只看潮汐。\n",
        encoding="utf-8",
    )
    cards = extract_cards(src.parent)["cards"]
    joined = "\n".join(c["body"] for c in cards)
    assert "越界没有豁免" in joined


def test_extract_hard_heading_includes_table(tmp_path: Path):
    src = tmp_path / "kb"
    src.mkdir()
    (src / "x01.md").write_text(
        "\n".join(
            [
                "# 层级与上限",
                "",
                "> 硬度：混合",
                "",
                "## 定义",
                "",
                "本世界视角的层级阶梯。",
                "",
                "## 细则",
                "",
                "### 【硬】层级上限表",
                "",
                "| 层级 | 上限 |",
                "|---|---|",
                "| 基础 | 60–100 |",
                "| 一阶 | 100–120 |",
                "",
                "一阶固定三层圆满后方可升阶。",
                "",
                "- 【软】这段读感不要进硬卡。",
                "- **失败**：伤根基，数年难再冲。",
                "",
                "### 【软】可写场景钩子",
                "",
                "不要抽这句闲笔钩子。",
            ]
        ),
        encoding="utf-8",
    )
    cards = extract_cards(src, excerpt_max=800)["cards"]
    titles = [c["title"] for c in cards]
    joined = "\n".join(c["body"] for c in cards)
    assert any("层级上限表" in t for t in titles)
    assert "60–100" in joined
    assert "100–120" in joined
    assert "三层圆满" in joined
    assert "伤根基" in joined
    assert "这段读感不要进硬卡" not in joined
    assert "不要抽这句闲笔钩子" not in joined


def test_extract_naming_rules_dir_not_skipped_as_roster(tmp_path: Path):
    src = tmp_path / "kb"
    naming = src / "设定" / "命名规则"
    naming.mkdir(parents=True)
    (naming / "N05.md").write_text(
        "\n".join(
            [
                "# 组织制度双轨命名",
                "",
                "## 定义",
                "",
                "正式公共机构名只用北门学馆。",
                "",
                "### 【硬】组织与机构（正式名；废名勿写回）",
                "",
                "| 作者层 | 世界内正式名 |",
                "|---|---|",
                "| 教学 | **北门学馆** |",
            ]
        ),
        encoding="utf-8",
    )
    roster = src / "人物名录"
    roster.mkdir()
    (roster / "list.md").write_text("# 人物名录\n\n- 主角：码头伙计\n", encoding="utf-8")
    result = extract_cards(src, excerpt_max=400)
    joined = "\n".join(c["body"] for c in result["cards"])
    titles = [c["title"] for c in result["cards"]]
    assert "北门学馆" in joined
    assert result["skipped_roster"] >= 1
    assert "人物名录" not in titles
    assert not any("主角：码头伙计" in (c.get("body") or "") for c in result["cards"])


def test_extract_default_soft_definition_not_hard(tmp_path: Path):
    src = tmp_path / "kb"
    src.mkdir()
    (src / "x14.md").write_text(
        "\n".join(
            [
                "# 世界写作易踩坑",
                "",
                "> 硬度：软",
                "",
                "## 定义",
                "",
                "> 本条默认【软】：可被剧情例外／阵营差异覆盖。",
                "",
                "写作时常见串线错误；写完对照查一遍。这段不应进硬库。",
                "",
                "## 边界与禁则",
                "",
                "- 本表不替代各条权威细则。",
            ]
        ),
        encoding="utf-8",
    )
    cards = extract_cards(src, excerpt_max=400)["cards"]
    def_cards = [c for c in cards if c["title"] == "定义"]
    assert def_cards == []
    joined = "\n".join(c["body"] for c in cards)
    assert "这段不应进硬库" not in joined


def test_extract_default_soft_keeps_explicit_hard_units(tmp_path: Path):
    src = tmp_path / "kb"
    src.mkdir()
    (src / "n01.md").write_text(
        "\n".join(
            [
                "## 定义",
                "",
                "> 本条默认【软】：可被剧情例外覆盖。",
                "",
                "取名总则是写作工具，这段软文不应整段进硬库。",
                "",
                "【硬】一阶分层用三层圆满，勿写十三层。",
            ]
        ),
        encoding="utf-8",
    )
    cards = extract_cards(src, excerpt_max=400)["cards"]
    joined = "\n".join(c["body"] for c in cards)
    assert "三层圆满" in joined
    assert "这段软文不应整段进硬库" not in joined


def test_init_missing_protagonist_leaves_no_half_project(tmp_path: Path):
    """plan 缺 protagonist 在建任何目录之前就要拒绝，不留半个项目。"""
    proj = tmp_path / "halfproj"
    plan = tmp_path / "bad_plan.json"
    plan.write_text(json.dumps({"chapters": [{"chapter": 1, "beats": []}]}), encoding="utf-8")
    try:
        init_project(proj, plan_path=plan)
        raised = False
    except LedgerError as exc:
        raised = exc.code == "missing_protagonist"
    assert raised
    assert not (proj / "book" / "run" / "LOCK").exists(), "被拒的 init 不应留下 LOCK 空壳"
    assert not (proj / "book" / "HEAD.json").exists()


def test_init_invalid_kb_leaves_no_half_project(tmp_path: Path):
    """kb JSON 形状非法同样在建目录之前拒绝。"""
    proj = tmp_path / "halfproj2"
    kb = tmp_path / "bad_kb.json"
    kb.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    try:
        init_project(proj, kb_path=kb, protagonist="张三")
        raised = False
    except LedgerError as exc:
        raised = exc.code == "invalid_kb"
    assert raised
    assert not (proj / "book" / "run" / "LOCK").exists()
