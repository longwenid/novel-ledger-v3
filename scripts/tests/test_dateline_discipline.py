"""时间呈现纪律（dateline_opening）回归测试。

探因（长跑自然收敛实测）：worker 每章都是空上下文会话，简报/正典灌满绝对日期
+ 机检盯时间线，写者的安全牌收敛成每章以「X月X号早上X点」日志句开场，连续数十
章后读感从小说滑向工作日志；且固定时刻（如「早上七点五十」）反复出现，机械感翻倍。

锁定的不变量：
- 分类器：日期句/时刻公式开场命中，场景化开场与裸时刻（无上午/下午前缀）不命中；
- 写作简报定位段带时间呈现纪律行（allow 豁免时不带）；
- submit 咨询警告：单章 dateline_opening（NIT 级），连续 ≥3 章 streak 升级；
- precheck 独立字段暴露探针结果，不污染 problems（非空即 fail 的信号语义）。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.content.style_check import dateline_opening
from novel_ledger_core.content.views import render_writing_brief
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.control.pipeline import precheck_prose, submit_output
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import read_json
from tests.decoupled_helpers import advance_to_assembly


@pytest.fixture()
def store(tmp_path: Path) -> BookStore:
    plan = {
        "title": "",
        "protagonist": "主角",
        "volume_spine": "第一卷：时间呈现验证。",
        "chapters": [
            {
                "chapter": n,
                "volume": "vol-0001",
                "location": "办公室",
                "present": ["主角"],
                "beats": [{"id": f"b{n}", "required": True, "text": f"第{n}章对账", "must": "对账"}],
            }
            for n in range(1, 5)
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    project = tmp_path / "bookproj"
    project.mkdir()
    init_project(project, plan_path=plan_path, protagonist="主角", word_min=20, word_max=5000)
    return BookStore(project)


def test_classifier_hits_dateline_formulas_only() -> None:
    for opening in (
        "三月五号，早上七点五十。他下楼。",
        "四月十三号，星期一，早上八点四十，他到了收发室。",
        "下午两点半，他把提取单填好。",
        "一九九八年一月二十六号，早上七点四十。暖气烧得半死不活。",
        "二月二十号，头班车。",
        "傍晚六点一刻，他锁了门。",
    ):
        assert dateline_opening(opening), opening
    for opening in (
        "腊月的公安局，走廊尽头那间预审室最冷。",
        "班车到矿区的时候，天阴着。",
        "十一点过一刻，他停了笔。",  # 裸时刻无上午/下午前缀：场景内时间，不判
        "他把文件夹抱在怀里上了三楼。",
    ):
        assert dateline_opening(opening) is None, opening


def test_writing_brief_carries_time_discipline_unless_allowed(store: BookStore) -> None:
    pack = {"chapter": 3, "word_band": {"min": 20, "max": 5000}, "dateline_openings": "discourage"}
    brief = render_writing_brief(pack)
    assert "时间呈现纪律" in brief and "dateline_opening" in brief
    allowed = render_writing_brief({**pack, "dateline_openings": "allow"})
    assert "时间呈现纪律" not in allowed


def _submit_chapter(store: BookStore, chapter: int, prose: str) -> dict:
    advance_to_assembly(store, prose=prose, chapter=chapter)
    return submit_output(
        store,
        {
            "l1_summary": "本章对账。",
            "state_delta": {"named": ["主角"]},
            "memory": {"voice_concepts": []},
            "pack_hash": read_json(store.current_pack_path)["pack_hash"],
            "beats_hit": [f"b{chapter}"],
            "plot_findings": [],
        },
    )


def _ack_and_advance(store: BookStore, prose: str) -> None:
    from novel_ledger_core.control.pipeline import ack_read, chapter_next

    assert chapter_next(store)["action"] == "ack"
    ack_read(store, quotes=[prose[i:i + 8] for i in range(0, 24, 8)])
    assert chapter_next(store)["action"] == "draft"


def test_submit_warns_single_then_streak(store: BookStore) -> None:
    # 第 1 章：正常开场，无警告。
    prose1 = "他把文件夹抱在怀里上了三楼，办公室的门虚掩着。对账的册子摊在桌上。"
    r1 = _submit_chapter(store, 1, prose1)
    assert r1["verdict"] == "accepted"
    assert not [w for w in r1.get("warnings", []) if str(w.get("code", "")).startswith("dateline")]
    _ack_and_advance(store, prose1)
    # 第 2 章：日期句开场——单章 NIT。
    prose2 = "三月五号，早上七点五十。他下楼先去收发室，头一天对账的信到了，他把信收进挎包。"
    r2 = _submit_chapter(store, 2, prose2)
    assert r2["verdict"] == "accepted"
    codes = [w.get("code") for w in r2.get("warnings", [])]
    assert "dateline_opening" in codes and "dateline_opening_streak" not in codes
    _ack_and_advance(store, prose2)
    # 第 3 章：连续第二个日期开场（本章+前一章 = 2，未到 3，仍是单章 NIT）。
    prose3 = "三月六号，早上八点。他把对账的册子又翻了一遍，账目齐了，数目一条不差。"
    r3 = _submit_chapter(store, 3, prose3)
    assert r3["verdict"] == "accepted"
    codes3 = [w.get("code") for w in r3.get("warnings", [])]
    assert "dateline_opening" in codes3 and "dateline_opening_streak" not in codes3
    _ack_and_advance(store, prose3)
    # 第 4 章：三连——streak 升级点名。
    prose4 = "三月七号，早上八点。他把对账的结果誊了一遍，条目对上了，他把纸夹进卷里。"
    r4 = _submit_chapter(store, 4, prose4)
    assert r4["verdict"] == "accepted"
    streak = [w for w in r4.get("warnings", []) if w.get("code") == "dateline_opening_streak"]
    assert streak and streak[0]["streak"] == 3


def test_precheck_exposes_dateline_without_polluting_problems(store: BookStore) -> None:
    path = store.staging_dir / "probe.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("三月五号，早上七点五十。他下楼。\n\n后续正文足够长。", encoding="utf-8")
    result = precheck_prose(store, path, chapter=1)
    assert result["dateline_opening"] is not None
    assert not [p for p in result["problems"] if "dateline" in str(p)], (
        "advisory 不得进 problems（非空即 fail 信号）"
    )
    cfg = store.load_config()
    cfg["dateline_openings"] = "allow"
    store.save_config(cfg)
    result2 = precheck_prose(store, path, chapter=1)
    assert result2["dateline_opening"] is None
