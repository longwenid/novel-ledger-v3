"""组装简报（assemble brief）回归测试。

背景（样本书第 5/6 章会话复盘）：组装 worker 面对单行 JSON 包只能脚本分片转储
（每章 4–5 遍 ≈35K 字符）并翻 skill 目录考古输出契约（≈25K），单章累计
0.9–1.2M token。简报把核对输入与提交模板渲染成一份多行文本，worker 一次 read 全进。

锁定的不变量：
- assemble action 必带 assemble_brief_path，且该文件与视图同源落盘；
- 简报自带提交模板（pack_hash 预填、required 拍 id 预填）与读取纪律；
- 渲染是视图的确定性单向函数（同视图两次渲染逐字节一致）；
- 账本投影有界（>24 条截断并注明），不允许无限膨胀。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.content.views import make_assemble_view, render_assemble_brief
from novel_ledger_core.infra.store import BookStore
from tests.decoupled_helpers import advance_to_assembly


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：组装简报验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "账房",
                "present": ["主角", "掌柜"],
                "beats": [
                    {"id": f"b{n}", "required": True, "text": f"第{n}章对账", "must": "对账"},
                ],
            }
            for n in range(1, 3)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "bookproj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def test_assemble_action_carries_brief_path_and_file_is_on_disk(store: BookStore) -> None:
    action = advance_to_assembly(store)
    assert "assemble_brief_path" in action, "assemble action must point at the one-pass brief"
    brief_path = Path(action["assemble_brief_path"])
    assert brief_path == store.assemble_brief_path(1)
    assert brief_path.exists(), "brief must be written together with the assemble view"
    brief = brief_path.read_text(encoding="utf-8")
    # 读取纪律必须在简报头部自足：一次读全 / 禁转储 / 禁考古 / 引文交机检。
    assert "不要用脚本分片转储" in brief
    assert "不要翻 skill 安装目录" in brief
    assert "机检逐字校验" in brief
    # hint 同步把 brief 钉成第一读取面，且保留宿主 submit 唯一闸门语义。
    hint = str(action.get("hint") or "")
    assert "assemble_brief_path" in hint
    assert "do NOT dump the pack JSON" in hint
    assert "the HOST executes `chapter submit`" in hint


def test_brief_embeds_submit_template_with_prefilled_machine_fields(store: BookStore) -> None:
    action = advance_to_assembly(store)
    view = json.loads(Path(action["assemble_pack_path"]).read_text(encoding="utf-8"))
    brief = Path(action["assemble_brief_path"]).read_text(encoding="utf-8")
    # pack_hash 预填：worker 不再翻包找精确值。
    assert view["pack_hash"] in brief
    # required 拍 id 预填进 beats_hit 模板行。
    assert '"beats_hit": ["b1"]' in brief
    # 六键模板与字段形状自足：不读包也能写出合法 submit JSON 骨架。
    for key in ('"l1_summary"', '"state_delta"', '"memory"', '"pack_hash"', '"beats_hit"', '"plot_findings"'):
        assert key in brief, f"template must show {key}"
    assert '"facts"' in brief and '"hooks"' in brief and '"knowledge"' in brief
    # 拍点核对与名字白名单进简报：worker 无需再从包里挖。
    assert "第1章对账" in brief
    assert "主角" in brief


def test_brief_rendering_is_deterministic_and_view_derived(store: BookStore) -> None:
    action = advance_to_assembly(store)
    pack = json.loads(Path(action["assemble_pack_path"]).read_text(encoding="utf-8"))
    first = render_assemble_brief(make_assemble_view(pack))
    second = render_assemble_brief(make_assemble_view(pack))
    assert first == second
    assert first.startswith("# 第1章组装任务书")
    # 简报必须覆盖各核对分区（有内容才成节；第1章无前情，验证简报节允许缺席）。
    for section in ("## 拍点与硬锚点", "## 人物与连续性", "## 当前剧情状态", "## 正典规则"):
        assert section in first, f"brief missing section {section}"


def test_brief_carries_verification_brief_when_present() -> None:
    view = {
        "schema": "novel-ledger.view.assemble.v2",
        "view": "assemble",
        "chapter": 2,
        "pack_hash": "sha256:test",
        "write_contract": {"delta_schema": {}},
        "verification_brief": "- 上一章结尾原文是：主角收好凭据。",
    }
    brief = render_assemble_brief(view)
    assert "## 前情验证简报" in brief
    assert "主角收好凭据" in brief


def test_brief_ledger_refs_are_bounded() -> None:
    """帽测试用合成视图直测：pack 侧 caps（hooks=5 等）平时压不到 24 帽，
    这里构造 40 条伏笔的投影，验证简报的防线独立成立。"""
    view = {
        "schema": "novel-ledger.view.assemble.v2",
        "view": "assemble",
        "chapter": 9,
        "pack_hash": "sha256:test",
        "write_contract": {"delta_schema": {}},
        "ledger_refs": {
            "hooks": [
                {"id": f"h-{i}", "text": f"约定{i}", "status": "open", "due": 12}
                for i in range(40)
            ]
        },
    }
    brief = render_assemble_brief(view)
    rendered = [line for line in brief.splitlines() if line.startswith("- 未结伏笔：")]
    assert len(rendered) == 24, "ledger refs must be capped at 24 lines per group"
    assert "另有 16 条未列出" in brief
    assert "定向查询" in brief


def test_assemble_action_backfills_brief_for_preexisting_packs(store: BookStore) -> None:
    """旧书的包先于简报特性存在：action 派发时必须按盘上视图补渲染，路径永远可读。"""
    from novel_ledger_core.control.pipeline import chapter_next

    advance_to_assembly(store)
    store.assemble_brief_path(1).unlink()  # 模拟升级前的旧书：有包、无简报
    action = chapter_next(store)
    assert action["action"] == "assemble"
    brief_path = Path(action["assemble_brief_path"])
    assert brief_path.exists(), "assemble action must backfill the brief from the on-disk view"
    assert "## 提交模板" in brief_path.read_text(encoding="utf-8")
