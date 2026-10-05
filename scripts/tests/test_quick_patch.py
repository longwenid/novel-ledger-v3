from __future__ import annotations

import json
from pathlib import Path
try:
    import pytest
except ImportError:
    from tests import pytest_compat as pytest

from novel_ledger_core.control.cli import main
from novel_ledger_core.control.pipeline import (
    ack_read,
    chapter_next,
    stage_draft_submit,
    stage_polish_submit,
    submit_output,
)
from novel_ledger_core.control.bootstrap import init_project
from novel_ledger_core.infra.store import BookStore
from novel_ledger_core.infra.util import LedgerError, read_json, sha256_text

from tests.decoupled_helpers import advance_to_assembly


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "novel"
    plan = {
        "protagonist": "陈三两",
        "volume_spine": "卷一",
        "chapters": [
            {
                "chapter": 1,
                "title": "破境",
                "beats": [
                    {"scene": 1, "description": "地火室开门", "must": ["火脉"]},
                    {"scene": 2, "description": "冲穴", "must": ["经脉"]},
                    {"scene": 3, "description": "破关而出", "must": ["铜漏"]},
                ],
            }
        ],
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    init_project(proj, plan_path=plan_file, protagonist="陈三两")
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["plan_low_water"] = 0
    cfg["word_band_enforce"] = False
    cfg["polish"] = "on"  # 本文件测润色泳线；skill 默认只写作
    store.save_config(cfg)
    return proj


def _commit_ch1(store: BookStore, text: str) -> None:
    advance_to_assembly(store, prose=text, chapter=1)
    pack = read_json(store.assemble_pack_path(1))
    output = {
        "prose": text,
        "l1_summary": "第一章摘要：陈三两在地火室冲穴破境。",
        "state_delta": {
            "moves": [{"who": "陈三两", "to": "地火室"}],
            "facts": [{"who": "陈三两", "text": "陈三两冲穴"}],
            "debts": [],
            "hooks": [],
            "relations": [],
            "named": ["陈三两"],
            "new_names": [],
            "deaths": [],
        },
        "memory": {"voice_concepts": []},
        "pack_hash": pack["pack_hash"],
        "beats_hit": ["b1", "b2", "b3"],
    }
    submit_output(store, output)
    # ack commit
    chapter_next(store)
    # 取三句作为 quote
    quotes = [s.strip() for s in text.split("。") if len(s.strip()) >= 6][:3]
    ack_read(store, quotes=quotes, verdict="pass")


def test_patch_staging_prose(tmp_path: Path):
    """测试暂存区（staging）段落修润：就地替换 polished.txt 并顺利推进流水线。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    r1 = chapter_next(store)
    # 草稿给润色提供事件骨架，二者不必等长（删注水、补细节是润色的正常职责）
    draft = "陈三两在火室门口站了许久。门外黄管事冷冷看着他，眼神冰冷。" + "这火脉极不稳定。铜漏里的砂子在淌。经脉疼痛欲裂。" * 3
    Path(r1["draft_output_path"]).write_text(draft, encoding="utf-8")
    stage_draft_submit(store)

    r2 = chapter_next(store)
    # 保证待替换目标在全文中唯一
    original_polished = "陈三两在火室门口站了许久。门外黄管事冷冷看着他，眼神如刀。" + "这火脉极不稳定。铜漏里的砂子在淌。经脉疼痛欲裂。" * 3
    Path(r2["polished_output_path"]).write_text(original_polished, encoding="utf-8")

    # 执行段落 patch
    target = "眼神如刀。"
    replacement = "嘴角微微抽搐。"
    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--target", target,
        "--replacement", replacement,
    ])
    assert code == 0

    new_polished = Path(r2["polished_output_path"]).read_text(encoding="utf-8")
    assert target not in new_polished
    assert replacement in new_polished

    # 推进到组装
    res = stage_polish_submit(store)
    assert res["verdict"] == "polish_accepted"


def test_patch_committed_prose_auto_resyncs(tmp_path: Path):
    """测试已入账（committed）正文段落修润：自动更新正文并一键自愈哈希与 quotes。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    ch_file = store.chapter_md_path(1)
    target = "经脉灼热翻腾。"
    replacement = "经脉中的气终于通到了底。"

    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", target,
        "--replacement", replacement,
    ])
    assert code == 0

    new_prose = ch_file.read_text(encoding="utf-8")
    assert target not in new_prose
    assert replacement in new_prose

    # 验证哈希已自愈
    new_hash = "sha256:" + sha256_text(new_prose)
    meta = read_json(store.meta_path(1))
    assert meta["prose_hash"] == new_hash

    ack = read_json(store.ack_path(1))
    assert ack["prose_hash"] == new_hash
    for q in ack["quotes"]:
        assert q in new_prose


def test_patch_committed_cannot_erase_required_beat_must(tmp_path: Path, capsys):
    """已入账章节目录修补后自动复验 beats/must：替换不能把本章必发生事件的关键词抹掉。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", "经脉灼热翻腾。",
        "--replacement", "气终于通了。",
    ])
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "patch_anchor_failed"
    # 原文未被改动
    assert "经脉灼热翻腾。" in store.chapter_md_path(1).read_text(encoding="utf-8")


def test_patch_target_not_found(tmp_path: Path, capsys):
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", "绝对不存在于正文中的一段文字",
        "--replacement", "替换内容",
    ])
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "patch_target_not_found"


def test_patch_style_hard_check_blocks_meta_narration(tmp_path: Path, capsys):
    """快速修润引入明显出戏的元叙述时，硬闸仍保护正文。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    cfg = store.load_config()
    cfg["style_check"] = True
    store.save_config(cfg)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    target = "这火脉极不稳定。"
    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", target,
        "--replacement", "这火脉极不稳定。值得注意的是，热浪翻腾。",
    ])
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "patch_style_hard_failed"
    # 被拒的 patch 绝不能留在盘上：账本一致性探针会临时写入 patch 后的正文，
    # 若这里不还原，就会出现「patch 报错、正文却已改」的假阴性，绕过所有闸门。
    prose = store.chapter_md_path(1).read_text(encoding="utf-8")
    assert target in prose
    assert "值得注意的是" not in prose


def test_patch_via_json_file(tmp_path: Path):
    """测试通过 JSON 文件批量应用 patch。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    ch_file = store.chapter_md_path(1)
    t1 = "这火脉极不稳定。"
    r1 = "地底火脉起伏难定。"

    patch_file = tmp_path / "patch.json"
    patch_file.write_text(json.dumps([{"target": t1, "replacement": r1}], ensure_ascii=False), encoding="utf-8")

    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--patch-file", str(patch_file),
    ])
    assert code == 0

    new_prose = ch_file.read_text(encoding="utf-8")
    assert r1 in new_prose


def test_patch_committed_prose_without_explicit_chapter_flag(tmp_path: Path):
    """验证已入账章节在省略--chapter时准确命中committed正文，且暂存区在commit后自动GC。"""
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    # 1. 验证暂存区临时文件在 commit 后已被自动清理
    assert not store.draft_text_path(1).exists()
    assert not store.polished_text_path(1).exists()
    assert not store.output_path.exists()

    # 2. 模拟即便存在孤儿/残渣 staging 润色稿，也不影响已入账章节优先命中 committed 正文
    stale_polished = tmp_path / "stale_polished.txt"
    stale_polished.write_text("这是被遗弃的旧暂存正文。这火脉极不稳定。", encoding="utf-8")
    # 放置残渣文件到 staging
    store.staging_dir.mkdir(parents=True, exist_ok=True)
    store.polished_text_path(1).write_text(stale_polished.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. 不带 --chapter 执行 patch
    target = "这火脉极不稳定。"
    replacement = "地底火脉咆哮不休。"
    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--target", target,
        "--replacement", replacement,
    ])
    assert code == 0

    # 4. 验证真正入账的正文已被修改
    ch_file = store.chapter_md_path(1)
    new_prose = ch_file.read_text(encoding="utf-8")
    assert replacement in new_prose
    assert target not in new_prose

    # 5. 验证基线元数据哈希已自愈同步
    new_hash = "sha256:" + sha256_text(new_prose)
    meta = read_json(store.meta_path(1))
    assert meta["prose_hash"] == new_hash



def test_patch_uses_current_glossary_not_archived_pack(tmp_path: Path, capsys):
    """老章节就地修润时，术语闸必须以**当前 config.glossary** 为准，而非写作时的归档 pack。

    回归：归档 pack 的 glossary 是旧快照，项目后加的禁用写法不在其中，patch 会漏；
    表现为新守卫能护住新章节、却护不住老章节（只剩 book audit 事后能抓）。
    """
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    # 该章写作时没有这条禁用词；之后项目才加，且它只出现在 replacement 里
    cfg = store.load_config()
    cfg["glossary"] = {"滚烫如烙": "发烫"}
    store.save_config(cfg)

    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", "门外黄管事冷冷看着他。",
        "--replacement", "门外黄管事冷冷看着他，石壁滚烫如烙。",
    ])
    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "patch_anchor_failed"
    assert any(i.get("code") == "glossary_term_banned" for i in payload["error"]["details"]["issues"])
    # 原文未被改动
    assert "石壁滚烫如烙" not in store.chapter_md_path(1).read_text(encoding="utf-8")


def test_patch_tolerates_preexisting_glossary_violation(tmp_path: Path):
    """老章节在闸门上线**前**就已含禁用词（存量债）时，不该让任何无关修补被卡死。

    只拦本次 patch 新引入的违规；存量由 book audit / book reconcile 记 advisory。
    """
    proj = _make_project(tmp_path)
    store = BookStore(proj)
    base_text = "陈三两在火室门口站了许久。门外黄管事冷冷看着他。这火脉极不稳定。铜漏里的赤金砂淌得极快。经脉灼热翻腾。" + "其余门人屏息凝神，不敢多言半句。地火蒸腾，石壁微微发烫。" * 5
    _commit_ch1(store, base_text)

    cfg = store.load_config()
    cfg["glossary"] = {"经脉灼热翻腾": "经脉翻腾"}  # 正文里本就含它（存量）
    store.save_config(cfg)

    # 修一处与禁用词无关的句子 → 应放行
    code = main([
        "chapter", "patch",
        "--project", str(proj),
        "--chapter", "1",
        "--target", "铜漏里的赤金砂淌得极快。",
        "--replacement", "铜漏里的赤金砂淌得慢了些。",
    ])
    assert code == 0
    assert "淌得慢了些" in store.chapter_md_path(1).read_text(encoding="utf-8")
